"""Phase 3 single-writer asynchronous DAG executor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from math import ceil
from typing import Any
from uuid import uuid4

from researchos.application.budget_ledger import reserve, settle
from researchos.application.checkpoint_manager import CheckpointManager
from researchos.application.errors import (
    CheckpointCompatibilityError,
    RuntimePreconditionError,
)
from researchos.application.runtime_transitions import (
    replace_task,
    stable_key,
    validate_attempt_transition,
    validate_task_transition,
)
from researchos.domain.contracts import RunState, RunStatus, TraceEvent, TraceEventType
from researchos.domain.planning import ReplanContext, ReplanRequest, TaskDAG
from researchos.domain.runtime import (
    TASK_TERMINAL_STATUSES,
    AttemptStatus,
    CheckpointMutation,
    DurableReplanState,
    ExecutionPolicy,
    ExecutionResultStatus,
    ExecutionSummary,
    ExecutorStatus,
    IdempotencyMode,
    RuntimeBudgetState,
    RuntimeCheckpoint,
    RuntimePauseReason,
    RuntimeResourceAmount,
    TaskAttempt,
    TaskExecutionRequest,
    TaskExecutionResult,
    TaskOutcome,
    TaskRuntimePolicy,
    TaskRuntimeState,
    TaskStatus,
    UsageCertainty,
)
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.runtime import (
    AsyncSleeper,
    RunCancellationController,
    TaskExecutionBackend,
)
from researchos.security.redaction import PersistenceRedactor

IdFactory = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class _RunningDispatch:
    sequence: int
    task_id: str
    policy: TaskRuntimePolicy
    future: asyncio.Task[object]


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class AsyncDAGExecutor:
    """Deterministic coordinator; backend coroutines never mutate state."""

    def __init__(
        self,
        *,
        checkpoints: CheckpointManager,
        backend: TaskExecutionBackend,
        clock: Clock,
        sleeper: AsyncSleeper,
        cancellation: RunCancellationController,
        id_factory: IdFactory | None = None,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._checkpoints = checkpoints
        self._backend = backend
        self._clock = clock
        self._sleeper = sleeper
        self._cancellation = cancellation
        self._id_factory = id_factory or _default_id_factory
        self._redactor = redactor or PersistenceRedactor()

    def initialize(
        self,
        state: RunState,
        dag: TaskDAG,
        policy: ExecutionPolicy,
        *,
        replan_context: ReplanContext,
    ) -> RuntimeCheckpoint:
        if state.status is not RunStatus.RUNNING:
            raise RuntimePreconditionError("executor requires RunStatus.RUNNING")
        if dag.run_id != state.run_id:
            raise RuntimePreconditionError("DAG belongs to another run")
        if dag.run_revision > state.revision:
            raise RuntimePreconditionError("DAG revision is newer than run state")
        dag_ids = {task.task_id for task in dag.tasks}
        if {item.task_id for item in policy.tasks} != dag_ids:
            raise RuntimePreconditionError(
                "execution policy must cover the DAG exactly"
            )
        now = self._clock.now()
        mutation_id = self._new_id("mutation")
        checkpoint = RuntimeCheckpoint(
            checkpoint_revision=0,
            run_id=state.run_id,
            run_revision=state.revision,
            dag_id=dag.dag_id,
            plan_id=dag.plan_id,
            dag=dag,
            dag_hash=self._hash(dag),
            execution_policy=policy,
            execution_policy_hash=self._hash(policy),
            executor_status=ExecutorStatus.INITIALIZED,
            task_states=tuple(
                TaskRuntimeState(task_id=task_id) for task_id in dag.topological_order
            ),
            budget=RuntimeBudgetState(
                limits=RuntimeResourceAmount(
                    duration_milliseconds=state.budget.limits.max_duration_seconds
                    * 1_000,
                    tokens=state.budget.limits.max_tokens,
                    cost_microunits=state.budget.limits.max_cost_microunits,
                    tool_calls=state.budget.limits.max_tool_calls,
                ),
                consumed=RuntimeResourceAmount(
                    duration_milliseconds=state.budget.usage.elapsed_milliseconds,
                    tokens=state.budget.usage.tokens,
                    cost_microunits=state.budget.usage.cost_microunits,
                    tool_calls=state.budget.usage.tool_calls,
                ),
            ),
            replan=DurableReplanState(
                context=replan_context,
                max_replans=policy.max_replans,
            ),
            last_mutation=CheckpointMutation(
                mutation_id=mutation_id, kind="initialized", events=()
            ),
            created_at=now,
            updated_at=now,
        )
        return self._checkpoints.create(checkpoint)

    async def execute(self, state: RunState) -> ExecutionSummary:
        checkpoint = self.resume(state)
        if checkpoint.executor_status in {
            ExecutorStatus.COMPLETED,
            ExecutorStatus.CANCELLED,
        }:
            return self._summary(checkpoint)
        if checkpoint.executor_status is ExecutorStatus.PAUSED:
            raise RuntimePreconditionError(
                "paused executor requires an external decision"
            )
        if checkpoint.executor_status is ExecutorStatus.INITIALIZED:
            checkpoint = self._mutation(
                checkpoint,
                kind="executor_started",
                executor_status=ExecutorStatus.RUNNING,
                event_specs=[(TraceEventType.EXECUTOR_STARTED, {})],
            )

        policy_by_task = {
            item.task_id: item for item in checkpoint.execution_policy.tasks
        }
        running: dict[asyncio.Task[object], _RunningDispatch] = {}
        dispatch_sequence = 0
        budget_breached = checkpoint.budget.breached
        while checkpoint.executor_status is ExecutorStatus.RUNNING:
            if self._cancellation.cancellation_requested:
                checkpoint = self._begin_run_cancellation(checkpoint)
                if not running:
                    checkpoint = self._finalize_run_cancellation(checkpoint)
                    break
            if budget_breached and not running:
                checkpoint = self._mutation(
                    checkpoint,
                    kind="budget_breached_pause",
                    executor_status=ExecutorStatus.PAUSED,
                    pause_reason=RuntimePauseReason.BUDGET_BREACHED,
                    event_specs=[
                        (TraceEventType.BUDGET_BREACHED, {}),
                        (
                            TraceEventType.EXECUTOR_PAUSED,
                            {"reason": RuntimePauseReason.BUDGET_BREACHED.value},
                        ),
                    ],
                )
                break

            checkpoint = self._promote_due_retries(checkpoint)
            checkpoint = self._propagate_blocked(checkpoint)
            checkpoint = self._mark_ready(checkpoint)
            if all(
                item.status in TASK_TERMINAL_STATUSES for item in checkpoint.task_states
            ):
                if self._cancellation.cancellation_requested:
                    checkpoint = self._finalize_run_cancellation(checkpoint)
                    break
                checkpoint = self._mutation(
                    checkpoint,
                    kind="executor_completed",
                    executor_status=ExecutorStatus.COMPLETED,
                    event_specs=[(TraceEventType.EXECUTOR_COMPLETED, {})],
                )
                break

            dispatched_any = False
            if not self._cancellation.cancellation_requested and not budget_breached:
                for task_state in self._ordered_ready(checkpoint):
                    if len(running) >= checkpoint.execution_policy.max_concurrency:
                        break
                    task_policy = policy_by_task[task_state.task_id]
                    if not task_policy.reservation.fits_within(
                        checkpoint.budget.available()
                    ):
                        continue
                    checkpoint, request = self._start_attempt(
                        checkpoint, task_state, task_policy
                    )
                    future = asyncio.create_task(
                        self._run_attempt(request, task_policy)
                    )
                    running[future] = _RunningDispatch(
                        sequence=dispatch_sequence,
                        task_id=task_state.task_id,
                        policy=task_policy,
                        future=future,
                    )
                    dispatch_sequence += 1
                    dispatched_any = True

            retry_delay = self._next_retry_delay(checkpoint)
            if not running:
                ready = self._ordered_ready(checkpoint)
                if retry_delay is None:
                    if ready and not dispatched_any:
                        checkpoint = self._mutation(
                            checkpoint,
                            kind="budget_exhausted",
                            executor_status=ExecutorStatus.PAUSED,
                            pause_reason=RuntimePauseReason.BUDGET_EXHAUSTED,
                            event_specs=[
                                (
                                    TraceEventType.EXECUTOR_PAUSED,
                                    {
                                        "reason": (
                                            RuntimePauseReason.BUDGET_EXHAUSTED.value
                                        )
                                    },
                                )
                            ],
                        )
                        break
                    if not ready:
                        raise RuntimePreconditionError(
                            "runtime has no ready, retry-wait, or running work"
                        )

            completed = await self._wait_for_progress(running, retry_delay)
            for dispatch in completed:
                running.pop(dispatch.future)
                try:
                    result: object = dispatch.future.result()
                except BaseException as exc:  # backend/task boundary
                    result = exc
                checkpoint = self._finish_attempt(
                    checkpoint, dispatch.task_id, dispatch.policy, result
                )
                if checkpoint.budget.breached:
                    budget_breached = True
        return self._summary(checkpoint)

    def resume(self, state: RunState) -> RuntimeCheckpoint:
        if state.status is not RunStatus.RUNNING:
            raise CheckpointCompatibilityError("runtime resume requires a running run")
        checkpoint = self._checkpoints.load_and_reconcile(state.run_id)
        if checkpoint.run_revision != state.revision:
            raise CheckpointCompatibilityError("run revision differs from checkpoint")
        if checkpoint.dag.run_id != state.run_id:
            raise CheckpointCompatibilityError("checkpoint DAG belongs to another run")
        if checkpoint.dag.run_revision > checkpoint.run_revision:
            raise CheckpointCompatibilityError("checkpoint DAG revision is impossible")
        if checkpoint.dag_hash != self._hash(checkpoint.dag):
            raise CheckpointCompatibilityError("checkpoint DAG hash differs")
        if checkpoint.execution_policy_hash != self._hash(checkpoint.execution_policy):
            raise CheckpointCompatibilityError("checkpoint policy hash differs")
        was_running = checkpoint.executor_status is ExecutorStatus.RUNNING
        running = [
            task for task in checkpoint.task_states if task.status is TaskStatus.RUNNING
        ]
        for task in sorted(running, key=lambda item: item.task_id):
            checkpoint = self._interrupt_attempt(checkpoint, task.task_id)
        if was_running:
            checkpoint = self._mutation(
                checkpoint,
                kind="executor_resumed",
                event_specs=[(TraceEventType.EXECUTOR_RESUMED, {})],
            )
        return checkpoint

    def request_cancel(self) -> None:
        """Explicit run-level command, separate from attempt signal objects."""

        self._cancellation.request_cancel()

    def request_replan(
        self,
        state: RunState,
        *,
        reason_code: str,
        reason: str,
    ) -> ReplanRequest:
        checkpoint = self.resume(state)
        replan = checkpoint.replan
        if (
            replan.pending_request is not None
            or replan.slots_used >= replan.max_replans
        ):
            event_type = TraceEventType.RUNTIME_REPLAN_REJECTED
            self._mutation(
                checkpoint,
                kind="replan_rejected",
                event_specs=[(event_type, {"reason_code": reason_code})],
            )
            raise RuntimePreconditionError("durable replan request is not available")
        safe_reason = self._redactor.redact_value(reason)
        assert isinstance(safe_reason, str)
        request = ReplanRequest(
            request_id=self._new_id("rpreq"),
            run_id=checkpoint.run_id,
            context=replan.context,
            reason_code=reason_code,
            reason=safe_reason,
        )
        new_replan = replan.model_copy(
            update={"slots_used": replan.slots_used + 1, "pending_request": request}
        )
        self._mutation(
            checkpoint,
            kind="replan_requested",
            executor_status=ExecutorStatus.PAUSED,
            pause_reason=RuntimePauseReason.REPLAN_REQUESTED,
            replan=new_replan,
            event_specs=[
                (
                    TraceEventType.RUNTIME_REPLAN_REQUESTED,
                    {
                        "request_id": request.request_id,
                        "reason_code": reason_code,
                        "slots_used": new_replan.slots_used,
                    },
                ),
                (
                    TraceEventType.EXECUTOR_PAUSED,
                    {"reason": RuntimePauseReason.REPLAN_REQUESTED.value},
                ),
            ],
        )
        return request

    def _start_attempt(
        self,
        checkpoint: RuntimeCheckpoint,
        task_state: TaskRuntimeState,
        policy: TaskRuntimePolicy,
    ) -> tuple[RuntimeCheckpoint, TaskExecutionRequest]:
        validate_task_transition(task_state.status, TaskStatus.RUNNING)
        attempt_number = len(task_state.attempts) + 1
        operation_key = stable_key(
            {
                "run_id": checkpoint.run_id,
                "dag_id": checkpoint.dag_id,
                "dag_hash": checkpoint.dag_hash,
                "task_id": task_state.task_id,
                "operation_version": policy.operation_version,
            }
        )
        attempt_key = stable_key(
            {"operation_key": operation_key, "attempt_number": attempt_number}
        )
        attempt_id = self._new_id("attempt")
        now = self._clock.now()
        attempt = TaskAttempt(
            attempt_id=attempt_id,
            task_id=task_state.task_id,
            attempt_number=attempt_number,
            operation_key=operation_key,
            attempt_key=attempt_key,
            status=AttemptStatus.RUNNING,
            started_at=now,
        )
        replacement = task_state.model_copy(
            update={
                "status": TaskStatus.RUNNING,
                "attempts": task_state.attempts + (attempt,),
                "next_eligible_at": None,
            }
        )
        new_budget = reserve(checkpoint.budget, policy.reservation)
        checkpoint = self._mutation(
            checkpoint,
            kind="attempt_started",
            task_states=replace_task(checkpoint, replacement),
            budget=new_budget,
            event_specs=[
                (
                    TraceEventType.BUDGET_RESERVED,
                    {
                        "task_id": task_state.task_id,
                        "amount": policy.reservation.model_dump(),
                    },
                ),
                (
                    TraceEventType.ATTEMPT_STARTED,
                    {
                        "task_id": task_state.task_id,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "operation_key": operation_key,
                        "attempt_key": attempt_key,
                    },
                ),
                (TraceEventType.TASK_STARTED, {"task_id": task_state.task_id}),
            ],
        )
        previous_receipts = [
            item.backend_receipt for item in task_state.attempts if item.backend_receipt
        ]
        request = TaskExecutionRequest(
            run_id=checkpoint.run_id,
            run_revision=checkpoint.run_revision,
            dag_id=checkpoint.dag_id,
            dag_hash=checkpoint.dag_hash,
            task=next(
                item
                for item in checkpoint.dag.tasks
                if item.task_id == task_state.task_id
            ),
            task_id=task_state.task_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            operation_key=operation_key,
            attempt_key=attempt_key,
            operation_version=policy.operation_version,
            task_idempotency=policy.idempotency,
            deadline=now + timedelta(milliseconds=policy.timeout_milliseconds),
            hard_limits=policy.reservation,
            prior_backend_receipt=previous_receipts[-1] if previous_receipts else None,
        )
        return checkpoint, request

    async def _run_attempt(
        self, request: TaskExecutionRequest, policy: TaskRuntimePolicy
    ) -> TaskExecutionResult | TimeoutError:
        backend_task = asyncio.create_task(
            self._backend.execute(request, self._cancellation.signal_for_attempt())
        )
        timeout_task = asyncio.create_task(
            self._sleeper.sleep(policy.timeout_milliseconds)
        )
        done, _ = await asyncio.wait(
            {backend_task, timeout_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if backend_task in done:
            timeout_task.cancel()
            with suppress(asyncio.CancelledError):
                await timeout_task
            return await backend_task
        backend_task.cancel()
        with suppress(asyncio.CancelledError):
            await backend_task
        return TimeoutError()

    def _finish_attempt(
        self,
        checkpoint: RuntimeCheckpoint,
        task_id: str,
        policy: TaskRuntimePolicy,
        raw_result: object,
    ) -> RuntimeCheckpoint:
        task = self._task(checkpoint, task_id)
        attempt = task.attempts[-1]
        now = self._clock.now()
        cancelled = self._cancellation.cancellation_requested
        if isinstance(raw_result, TimeoutError):
            status = AttemptStatus.TIMED_OUT
            result = TaskExecutionResult(
                status=ExecutionResultStatus.FAILED,
                failure_code="attempt_timeout",
                retryable=policy.retry_on_timeout,
                usage_certainty=UsageCertainty.UNKNOWN,
            )
        elif isinstance(raw_result, BaseException):
            status = AttemptStatus.FAILED
            result = TaskExecutionResult(
                status=ExecutionResultStatus.FAILED,
                failure_code="backend_exception",
                retryable=False,
                usage_certainty=UsageCertainty.UNKNOWN,
            )
        else:
            assert isinstance(raw_result, TaskExecutionResult)
            result = raw_result
            if result.status is ExecutionResultStatus.SUCCEEDED:
                planned_task = next(
                    item for item in checkpoint.dag.tasks if item.task_id == task_id
                )
                expected = {
                    output.output_id for output in planned_task.expected_outputs
                }
                if set(result.produced_output_ids) != expected:
                    result = TaskExecutionResult(
                        status=ExecutionResultStatus.FAILED,
                        usage=result.usage,
                        usage_certainty=result.usage_certainty,
                        retryable=False,
                        failure_code="backend_contract_violation",
                        backend_receipt=result.backend_receipt,
                    )
            if cancelled:
                status = AttemptStatus.CANCELLED
            elif result.status is ExecutionResultStatus.SUCCEEDED:
                status = AttemptStatus.SUCCEEDED
            else:
                status = AttemptStatus.FAILED
        new_budget = settle(
            checkpoint.budget,
            policy.reservation,
            usage=result.usage,
            certainty=result.usage_certainty,
        )
        settled_attempt = attempt.model_copy(
            update={
                "status": status,
                "finished_at": now,
                "usage": result.usage,
                "usage_certainty": result.usage_certainty,
                "failure_code": result.failure_code,
                "retryable": result.retryable,
                "backend_receipt": result.backend_receipt,
            }
        )
        validate_attempt_transition(attempt.status, status)
        attempts = task.attempts[:-1] + (settled_attempt,)
        retry_allowed = (
            not cancelled
            and result.status is ExecutionResultStatus.FAILED
            and result.retryable
            and policy.idempotency is IdempotencyMode.IDEMPOTENT
            and len(attempts) < policy.max_attempts
        )
        event_specs: list[tuple[TraceEventType, dict[str, Any]]] = [
            (
                TraceEventType.BUDGET_COMMITTED,
                {
                    "task_id": task_id,
                    "certainty": result.usage_certainty.value,
                    "usage": result.usage.model_dump() if result.usage else None,
                },
            )
        ]
        if result.usage_certainty is UsageCertainty.UNKNOWN:
            event_specs[0][1]["uncertain_consumption"] = policy.reservation.model_dump()
        else:
            event_specs.append((TraceEventType.BUDGET_RELEASED, {"task_id": task_id}))
        if status is AttemptStatus.SUCCEEDED:
            validate_task_transition(task.status, TaskStatus.SUCCEEDED)
            outcome = TaskOutcome(
                task_id=task_id,
                attempt_id=attempt.attempt_id,
                committed_at=now,
                produced_output_ids=result.produced_output_ids,
                backend_receipt=result.backend_receipt,
            )
            replacement = task.model_copy(
                update={
                    "status": TaskStatus.SUCCEEDED,
                    "attempts": attempts,
                    "outcome": outcome,
                }
            )
            event_specs.extend(
                [
                    (TraceEventType.ATTEMPT_SUCCEEDED, {"task_id": task_id}),
                    (TraceEventType.TASK_SUCCEEDED, {"task_id": task_id}),
                ]
            )
        elif cancelled:
            validate_task_transition(task.status, TaskStatus.CANCELLED)
            replacement = task.model_copy(
                update={"status": TaskStatus.CANCELLED, "attempts": attempts}
            )
            attempt_type = (
                TraceEventType.ATTEMPT_TIMED_OUT
                if status is AttemptStatus.TIMED_OUT
                else TraceEventType.ATTEMPT_CANCELLED
            )
            event_specs.extend(
                [
                    (attempt_type, {"task_id": task_id}),
                    (TraceEventType.TASK_CANCELLED, {"task_id": task_id}),
                ]
            )
        elif retry_allowed:
            validate_task_transition(task.status, TaskStatus.RETRY_WAIT)
            delay = policy.backoff_milliseconds[len(attempts) - 1]
            replacement = task.model_copy(
                update={
                    "status": TaskStatus.RETRY_WAIT,
                    "attempts": attempts,
                    "next_eligible_at": now + timedelta(milliseconds=delay),
                }
            )
            attempt_type = (
                TraceEventType.ATTEMPT_TIMED_OUT
                if status is AttemptStatus.TIMED_OUT
                else TraceEventType.ATTEMPT_FAILED
            )
            event_specs.extend(
                [
                    (attempt_type, {"task_id": task_id}),
                    (
                        TraceEventType.ATTEMPT_RETRY_SCHEDULED,
                        {"task_id": task_id, "backoff_milliseconds": delay},
                    ),
                ]
            )
        else:
            validate_task_transition(task.status, TaskStatus.FAILED)
            replacement = task.model_copy(
                update={"status": TaskStatus.FAILED, "attempts": attempts}
            )
            attempt_type = (
                TraceEventType.ATTEMPT_TIMED_OUT
                if status is AttemptStatus.TIMED_OUT
                else TraceEventType.ATTEMPT_FAILED
            )
            event_specs.extend(
                [
                    (attempt_type, {"task_id": task_id}),
                    (TraceEventType.TASK_FAILED, {"task_id": task_id}),
                ]
            )
        trace_order = {
            TraceEventType.ATTEMPT_SUCCEEDED: 0,
            TraceEventType.ATTEMPT_FAILED: 0,
            TraceEventType.ATTEMPT_TIMED_OUT: 0,
            TraceEventType.ATTEMPT_CANCELLED: 0,
            TraceEventType.BUDGET_COMMITTED: 1,
            TraceEventType.BUDGET_RELEASED: 2,
        }
        attempt_event_types = {
            TraceEventType.ATTEMPT_SUCCEEDED,
            TraceEventType.ATTEMPT_FAILED,
            TraceEventType.ATTEMPT_TIMED_OUT,
            TraceEventType.ATTEMPT_CANCELLED,
            TraceEventType.ATTEMPT_RETRY_SCHEDULED,
        }
        for event_type, attributes in event_specs:
            if event_type in attempt_event_types:
                attributes.update(
                    {
                        "attempt_id": attempt.attempt_id,
                        "attempt_number": attempt.attempt_number,
                        "failure_code": result.failure_code,
                        "retryable": result.retryable,
                    }
                )
        event_specs.sort(key=lambda item: trace_order.get(item[0], 3))
        return self._mutation(
            checkpoint,
            kind="attempt_settled",
            task_states=replace_task(checkpoint, replacement),
            budget=new_budget,
            event_specs=event_specs,
        )

    def _promote_due_retries(
        self, checkpoint: RuntimeCheckpoint
    ) -> RuntimeCheckpoint:
        now = self._clock.now()
        replacements = list(checkpoint.task_states)
        ready_ids: list[str] = []
        for index, task in enumerate(replacements):
            if (
                task.status is TaskStatus.RETRY_WAIT
                and task.next_eligible_at is not None
                and task.next_eligible_at <= now
            ):
                validate_task_transition(task.status, TaskStatus.READY)
                replacements[index] = task.model_copy(
                    update={"status": TaskStatus.READY, "next_eligible_at": None}
                )
                ready_ids.append(task.task_id)
        if not ready_ids:
            return checkpoint
        return self._mutation(
            checkpoint,
            kind="retries_ready",
            task_states=tuple(replacements),
            event_specs=[
                (TraceEventType.TASK_READY, {"task_id": task_id})
                for task_id in ready_ids
            ],
        )

    def _next_retry_delay(self, checkpoint: RuntimeCheckpoint) -> int | None:
        deadlines = [
            task.next_eligible_at
            for task in checkpoint.task_states
            if task.status is TaskStatus.RETRY_WAIT
            and task.next_eligible_at is not None
        ]
        if not deadlines:
            return None
        remaining = min(deadlines) - self._clock.now()
        return max(0, ceil(remaining.total_seconds() * 1_000))

    async def _wait_for_progress(
        self,
        running: dict[asyncio.Task[object], _RunningDispatch],
        retry_delay: int | None,
    ) -> list[_RunningDispatch]:
        retry_task = (
            asyncio.create_task(self._sleeper.sleep(retry_delay))
            if retry_delay is not None
            else None
        )
        waiters: set[asyncio.Task[object]] = set(running)
        cancellation_task: asyncio.Task[object] | None = None
        if not self._cancellation.cancellation_requested:
            cancellation_task = asyncio.create_task(
                self._cancellation.signal_for_attempt().wait()
            )
            waiters.add(cancellation_task)
        if retry_task is not None:
            waiters.add(retry_task)
        auxiliary: set[asyncio.Task[object]] = set()
        if cancellation_task is not None:
            auxiliary.add(cancellation_task)
        if retry_task is not None:
            auxiliary.add(retry_task)
        try:
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            for task in auxiliary:
                task.cancel()
            for task in auxiliary:
                with suppress(asyncio.CancelledError):
                    await task
            raise
        for task in auxiliary - done:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        completed = [running[future] for future in done if future in running]
        return sorted(completed, key=lambda item: (item.sequence, item.task_id))

    def _interrupt_attempt(
        self, checkpoint: RuntimeCheckpoint, task_id: str
    ) -> RuntimeCheckpoint:
        task = self._task(checkpoint, task_id)
        policy = self._policy(checkpoint, task_id)
        attempt = task.attempts[-1]
        now = self._clock.now()
        interrupted = attempt.model_copy(
            update={
                "status": AttemptStatus.INTERRUPTED,
                "finished_at": now,
                "usage": None,
                "usage_certainty": UsageCertainty.UNKNOWN,
                "failure_code": "process_interrupted",
                "retryable": policy.idempotency is IdempotencyMode.IDEMPOTENT,
            }
        )
        validate_attempt_transition(attempt.status, AttemptStatus.INTERRUPTED)
        can_retry = (
            policy.idempotency is IdempotencyMode.IDEMPOTENT
            and len(task.attempts) < policy.max_attempts
        )
        if can_retry:
            validate_task_transition(task.status, TaskStatus.RETRY_WAIT)
            replacement = task.model_copy(
                update={
                    "status": TaskStatus.RETRY_WAIT,
                    "attempts": task.attempts[:-1] + (interrupted,),
                    "next_eligible_at": now,
                }
            )
        else:
            validate_task_transition(task.status, TaskStatus.FAILED)
            replacement = task.model_copy(
                update={
                    "status": TaskStatus.FAILED,
                    "attempts": task.attempts[:-1] + (interrupted,),
                }
            )
        new_budget = settle(
            checkpoint.budget,
            policy.reservation,
            usage=None,
            certainty=UsageCertainty.UNKNOWN,
        )
        events = [
            (
                TraceEventType.ATTEMPT_INTERRUPTED,
                {
                    "task_id": task_id,
                    "attempt_id": attempt.attempt_id,
                    "attempt_number": attempt.attempt_number,
                    "failure_code": "process_interrupted",
                    "retryable": can_retry,
                },
            ),
            (
                TraceEventType.BUDGET_COMMITTED,
                {
                    "task_id": task_id,
                    "certainty": UsageCertainty.UNKNOWN.value,
                    "usage": None,
                    "uncertain_consumption": policy.reservation.model_dump(),
                },
            ),
        ]
        if not can_retry:
            events.append((TraceEventType.TASK_FAILED, {"task_id": task_id}))
        return self._mutation(
            checkpoint,
            kind="attempt_interrupted",
            task_states=replace_task(checkpoint, replacement),
            budget=new_budget,
            event_specs=events,
        )

    def _mark_ready(self, checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
        statuses = {item.task_id: item.status for item in checkpoint.task_states}
        task_by_id = {item.task_id: item for item in checkpoint.dag.tasks}
        replacements = list(checkpoint.task_states)
        ready_ids: list[str] = []
        for index, state in enumerate(replacements):
            if state.status is not TaskStatus.PENDING:
                continue
            dependencies = task_by_id[state.task_id].dependencies
            if all(
                statuses[item.task_id] is TaskStatus.SUCCEEDED for item in dependencies
            ):
                validate_task_transition(state.status, TaskStatus.READY)
                replacements[index] = state.model_copy(
                    update={"status": TaskStatus.READY}
                )
                ready_ids.append(state.task_id)
        if not ready_ids:
            return checkpoint
        return self._mutation(
            checkpoint,
            kind="tasks_ready",
            task_states=tuple(replacements),
            event_specs=[
                (TraceEventType.TASK_READY, {"task_id": task_id})
                for task_id in ready_ids
            ],
        )

    def _propagate_blocked(self, checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
        task_by_id = {item.task_id: item for item in checkpoint.dag.tasks}
        states = {item.task_id: item for item in checkpoint.task_states}
        changed: list[str] = []
        for task_id in checkpoint.dag.topological_order:
            state = states[task_id]
            if state.status is not TaskStatus.PENDING:
                continue
            roots: set[str] = set()
            for dependency in task_by_id[task_id].dependencies:
                dependency_state = states[dependency.task_id]
                if dependency_state.status is TaskStatus.BLOCKED:
                    roots.update(dependency_state.blocked_by)
                elif dependency_state.status in {
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    roots.add(dependency.task_id)
            if roots:
                validate_task_transition(state.status, TaskStatus.BLOCKED)
                states[task_id] = state.model_copy(
                    update={
                        "status": TaskStatus.BLOCKED,
                        "blocked_by": tuple(sorted(roots)),
                    }
                )
                changed.append(task_id)
        if not changed:
            return checkpoint
        ordered = tuple(states[item.task_id] for item in checkpoint.task_states)
        return self._mutation(
            checkpoint,
            kind="tasks_blocked",
            task_states=ordered,
            event_specs=[
                (
                    TraceEventType.TASK_BLOCKED,
                    {
                        "task_id": task_id,
                        "blocked_by": list(states[task_id].blocked_by),
                    },
                )
                for task_id in changed
            ],
        )

    def _begin_run_cancellation(
        self, checkpoint: RuntimeCheckpoint
    ) -> RuntimeCheckpoint:
        if checkpoint.cancellation_requested_at is not None:
            return checkpoint
        now = self._clock.now()
        states: list[TaskRuntimeState] = []
        events: list[tuple[TraceEventType, dict[str, Any]]] = [
            (TraceEventType.EXECUTOR_CANCEL_REQUESTED, {})
        ]
        for task in checkpoint.task_states:
            if task.status in {
                TaskStatus.PENDING,
                TaskStatus.READY,
                TaskStatus.RETRY_WAIT,
            }:
                validate_task_transition(task.status, TaskStatus.CANCELLED)
                states.append(
                    task.model_copy(
                        update={
                            "status": TaskStatus.CANCELLED,
                            "next_eligible_at": None,
                        }
                    )
                )
                events.append(
                    (TraceEventType.TASK_CANCELLED, {"task_id": task.task_id})
                )
            else:
                states.append(task)
        return self._mutation(
            checkpoint,
            kind="run_cancel_requested",
            task_states=tuple(states),
            cancellation_requested_at=now,
            event_specs=events,
        )

    def _finalize_run_cancellation(
        self, checkpoint: RuntimeCheckpoint
    ) -> RuntimeCheckpoint:
        if any(task.status is TaskStatus.RUNNING for task in checkpoint.task_states):
            raise RuntimePreconditionError(
                "cannot finalize cancellation while attempts are running"
            )
        return self._mutation(
            checkpoint,
            kind="run_cancelled",
            executor_status=ExecutorStatus.CANCELLED,
            event_specs=[],
        )

    def _ordered_ready(self, checkpoint: RuntimeCheckpoint) -> list[TaskRuntimeState]:
        topo = {
            task_id: index
            for index, task_id in enumerate(checkpoint.dag.topological_order)
        }
        priorities = {
            item.task_id: item.policy.priority for item in checkpoint.dag.tasks
        }
        return sorted(
            (
                item
                for item in checkpoint.task_states
                if item.status is TaskStatus.READY
            ),
            key=lambda item: (
                -priorities[item.task_id],
                topo[item.task_id],
                item.task_id,
            ),
        )

    def _mutation(
        self,
        checkpoint: RuntimeCheckpoint,
        *,
        kind: str,
        event_specs: list[tuple[TraceEventType, dict[str, Any]]],
        **updates: Any,
    ) -> RuntimeCheckpoint:
        mutation_id = self._new_id("mutation")
        now = self._clock.now()
        descriptors = tuple(
            self._checkpoints.descriptor(
                TraceEvent(
                    event_id=self._new_id("evt"),
                    event_type=event_type,
                    timestamp=now,
                    run_id=checkpoint.run_id,
                    revision=checkpoint.run_revision,
                    correlation_id=mutation_id,
                    attributes=self._safe_attributes(attributes),
                )
            )
            for event_type, attributes in event_specs
        )
        values = {
            "checkpoint_revision": checkpoint.checkpoint_revision + 1,
            "updated_at": now,
            "last_mutation": CheckpointMutation(
                mutation_id=mutation_id, kind=kind, events=descriptors
            ),
            **updates,
        }
        target = checkpoint.model_copy(update=values)
        # model_copy does not revalidate nested invariant changes in Pydantic.
        target = RuntimeCheckpoint.model_validate(target.model_dump(mode="python"))
        return self._checkpoints.commit(
            target, expected_revision=checkpoint.checkpoint_revision
        )

    def _safe_attributes(self, attributes: dict[str, Any]) -> dict[str, Any]:
        safe = self._redactor.redact_value(attributes)
        assert isinstance(safe, dict)
        return safe

    @staticmethod
    def _task(checkpoint: RuntimeCheckpoint, task_id: str) -> TaskRuntimeState:
        return next(item for item in checkpoint.task_states if item.task_id == task_id)

    @staticmethod
    def _policy(checkpoint: RuntimeCheckpoint, task_id: str) -> TaskRuntimePolicy:
        return next(
            item
            for item in checkpoint.execution_policy.tasks
            if item.task_id == task_id
        )

    @staticmethod
    def _hash(model: Any) -> str:
        from researchos.domain.contracts import model_sha256

        return model_sha256(model)

    @staticmethod
    def _summary(checkpoint: RuntimeCheckpoint) -> ExecutionSummary:
        counts = {status: 0 for status in TASK_TERMINAL_STATUSES}
        for task in checkpoint.task_states:
            if task.status in counts:
                counts[task.status] += 1
        return ExecutionSummary(
            run_id=checkpoint.run_id,
            checkpoint_revision=checkpoint.checkpoint_revision,
            executor_status=checkpoint.executor_status,
            succeeded=counts[TaskStatus.SUCCEEDED],
            failed=counts[TaskStatus.FAILED],
            blocked=counts[TaskStatus.BLOCKED],
            cancelled=counts[TaskStatus.CANCELLED],
        )

    def _new_id(self, prefix: str) -> str:
        return self._id_factory(prefix)
