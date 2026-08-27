from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace

from runtime_fixtures import RuntimeExample, RuntimeIds, runtime_example

from researchos.adapters.checkpoint_memory import InMemoryCheckpointStore
from researchos.adapters.mock_execution import (
    ExecutionFixture,
    ExecutionFixtureKey,
    MockAction,
    MockTaskExecutionBackend,
    successful_result,
)
from researchos.adapters.sleeper import AsyncioRunCancellationController
from researchos.application.async_dag_executor import AsyncDAGExecutor
from researchos.application.checkpoint_manager import CheckpointManager
from researchos.domain.contracts import TraceEventType
from researchos.domain.runtime import (
    AttemptStatus,
    ExecutionPolicy,
    ExecutionResultStatus,
    ExecutorStatus,
    RuntimePauseReason,
    RuntimeResourceAmount,
    TaskExecutionResult,
    TaskRuntimePolicy,
    TaskStatus,
    UsageCertainty,
)


class HangingSleeper:
    async def sleep(self, milliseconds: int) -> None:
        del milliseconds
        await asyncio.Future()


def _single_task(example: RuntimeExample) -> RuntimeExample:
    task = next(item for item in example.dag.tasks if item.task_id == "task_a")
    summary = example.dag.validation_summary.model_copy(
        update={
            "graph_depth": 1,
            "total_estimate": task.estimate,
            "critical_path_duration_milliseconds": task.estimate.duration_milliseconds,
            "task_count": 1,
        }
    )
    dag = example.dag.model_copy(
        update={
            "tasks": (task,),
            "topological_order": (task.task_id,),
            "validation_summary": summary,
        }
    )
    policy = example.policy.model_copy(
        update={
            "max_concurrency": 1,
            "tasks": tuple(
                item for item in example.policy.tasks if item.task_id == task.task_id
            ),
        }
    )
    return replace(example, dag=dag, policy=policy)


def _executor_parts(example: RuntimeExample, backend: object, sleeper: object):
    store = InMemoryCheckpointStore()
    ids = RuntimeIds()
    manager = CheckpointManager(
        store=store,
        trace_sink=example.trace,
        clock=example.clock,
        id_factory=ids,
    )
    cancellation = AsyncioRunCancellationController()
    executor = AsyncDAGExecutor(
        checkpoints=manager,
        backend=backend,  # type: ignore[arg-type]
        clock=example.clock,
        sleeper=sleeper,  # type: ignore[arg-type]
        cancellation=cancellation,
        id_factory=ids,
    )
    return executor, manager, store, cancellation, ids


def test_fast_attempt_is_checkpointed_before_slow_sibling_completes() -> None:
    class GatedBackend:
        def __init__(self) -> None:
            self.release_b = asyncio.Event()
            self.b_completed = False

        async def execute(self, request, cancellation):
            del cancellation
            if request.task_id == "task_b":
                await self.release_b.wait()
                self.b_completed = True
                outputs = ("risks",)
            elif request.task_id == "task_a":
                await asyncio.sleep(0)
                outputs = ("sources",)
            else:
                outputs = ("comparison",)
            return successful_result(output_ids=outputs)

    async def scenario() -> None:
        example = runtime_example(max_concurrency=2)
        backend = GatedBackend()
        executor, _, store, _, _ = _executor_parts(
            example, backend, HangingSleeper()
        )
        executor.initialize(
            example.state, example.dag, example.policy, replan_context=example.context
        )
        execution = asyncio.create_task(executor.execute(example.state))
        for _ in range(100):
            checkpoint = store.load(example.state.run_id)
            task_a = next(
                item for item in checkpoint.task_states if item.task_id == "task_a"
            )
            if task_a.status is TaskStatus.SUCCEEDED:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("fast task was not checkpointed")
        assert not backend.b_completed
        assert next(
            item for item in checkpoint.task_states if item.task_id == "task_b"
        ).status is TaskStatus.RUNNING
        backend.release_b.set()
        assert (await execution).executor_status is ExecutorStatus.COMPLETED

    asyncio.run(scenario())


def test_retry_wait_survives_restart_and_waits_only_remaining_backoff() -> None:
    class BlockingRetrySleeper:
        def __init__(self) -> None:
            self.retry_wait_started = asyncio.Event()

        async def sleep(self, milliseconds: int) -> None:
            if milliseconds < 10_000:
                self.retry_wait_started.set()
            await asyncio.Future()

    class AdvancingRetrySleeper:
        def __init__(self, example: RuntimeExample) -> None:
            self.example = example
            self.delays: list[int] = []

        async def sleep(self, milliseconds: int) -> None:
            if milliseconds >= 10_000:
                await asyncio.Future()
            self.delays.append(milliseconds)
            self.example.clock.advance(milliseconds=milliseconds)
            await asyncio.sleep(0)

    async def scenario() -> None:
        example = _single_task(
            runtime_example(max_attempts=2, timeout_milliseconds=10_000)
        )
        task_policy = example.policy.tasks[0].model_copy(
            update={"backoff_milliseconds": (1_000,)}
        )
        example = replace(
            example,
            policy=example.policy.model_copy(update={"tasks": (task_policy,)}),
        )
        backend = MockTaskExecutionBackend(
            {
                ExecutionFixtureKey("task_a", 1): ExecutionFixture(
                    result=TaskExecutionResult(
                        status=ExecutionResultStatus.FAILED,
                        failure_code="transient",
                        retryable=True,
                        usage=RuntimeResourceAmount(tokens=1),
                        usage_certainty=UsageCertainty.EXACT,
                    )
                ),
                ExecutionFixtureKey("task_a", 2): ExecutionFixture(
                    result=successful_result(output_ids=("sources",))
                ),
            }
        )
        first_sleeper = BlockingRetrySleeper()
        executor, manager, store, _, ids = _executor_parts(
            example, backend, first_sleeper
        )
        executor.initialize(
            example.state, example.dag, example.policy, replan_context=example.context
        )
        first_process = asyncio.create_task(executor.execute(example.state))
        await first_sleeper.retry_wait_started.wait()
        persisted = store.load(example.state.run_id)
        assert persisted.task_states[0].status is TaskStatus.RETRY_WAIT
        first_process.cancel()
        with suppress(asyncio.CancelledError):
            await first_process

        example.clock.advance(milliseconds=400)
        second_sleeper = AdvancingRetrySleeper(example)
        restarted = AsyncDAGExecutor(
            checkpoints=manager,
            backend=backend,
            clock=example.clock,
            sleeper=second_sleeper,
            cancellation=AsyncioRunCancellationController(),
            id_factory=ids,
        )
        summary = await restarted.execute(example.state)

        assert summary.executor_status is ExecutorStatus.COMPLETED
        assert second_sleeper.delays == [600]
        final = store.load(example.state.run_id).task_states[0]
        assert final.status is TaskStatus.SUCCEEDED
        assert len(final.attempts) == 2

    asyncio.run(scenario())


def test_run_cancellation_interrupts_durable_retry_wait() -> None:
    class BlockingRetrySleeper:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def sleep(self, milliseconds: int) -> None:
            if milliseconds < 10_000:
                self.started.set()
            await asyncio.Future()

    async def scenario() -> None:
        example = _single_task(
            runtime_example(max_attempts=2, timeout_milliseconds=10_000)
        )
        task_policy = example.policy.tasks[0].model_copy(
            update={"backoff_milliseconds": (1_000,)}
        )
        example = replace(
            example,
            policy=example.policy.model_copy(update={"tasks": (task_policy,)}),
        )
        backend = MockTaskExecutionBackend(
            {
                ExecutionFixtureKey("task_a", 1): ExecutionFixture(
                    result=TaskExecutionResult(
                        status=ExecutionResultStatus.FAILED,
                        failure_code="transient",
                        retryable=True,
                        usage=RuntimeResourceAmount(tokens=1),
                        usage_certainty=UsageCertainty.EXACT,
                    )
                )
            }
        )
        sleeper = BlockingRetrySleeper()
        executor, _, store, _, _ = _executor_parts(example, backend, sleeper)
        executor.initialize(
            example.state, example.dag, example.policy, replan_context=example.context
        )
        execution = asyncio.create_task(executor.execute(example.state))
        await sleeper.started.wait()
        assert (
            store.load(example.state.run_id).task_states[0].status
            is TaskStatus.RETRY_WAIT
        )
        executor.request_cancel()
        summary = await execution

        assert summary.executor_status is ExecutorStatus.CANCELLED
        assert (
            store.load(example.state.run_id).task_states[0].status
            is TaskStatus.CANCELLED
        )

    asyncio.run(scenario())


def test_ready_scan_skips_unreservable_tasks_and_dispatches_later_fit() -> None:
    example = runtime_example(max_concurrency=1)
    task_c = next(item for item in example.dag.tasks if item.task_id == "task_c")
    task_c = task_c.model_copy(update={"dependencies": ()})
    dag = example.dag.model_copy(
        update={
            "tasks": tuple(
                task_c if item.task_id == "task_c" else item
                for item in example.dag.tasks
            )
        }
    )
    limits = example.state.budget.limits
    policies: list[TaskRuntimePolicy] = []
    for item in example.policy.tasks:
        reservation = (
            RuntimeResourceAmount(
                duration_milliseconds=item.timeout_milliseconds,
                tokens=limits.max_tokens + 1,
            )
            if item.task_id in {"task_a", "task_b"}
            else RuntimeResourceAmount(
                duration_milliseconds=item.timeout_milliseconds, tokens=1
            )
        )
        policies.append(item.model_copy(update={"reservation": reservation}))
    policy = ExecutionPolicy(max_concurrency=1, tasks=tuple(policies))
    example = replace(example, dag=dag, policy=policy)
    backend = MockTaskExecutionBackend(
        {
            ExecutionFixtureKey("task_c", 1): ExecutionFixture(
                result=successful_result(output_ids=("comparison",))
            )
        }
    )
    executor, _, store, _, _ = _executor_parts(example, backend, HangingSleeper())
    executor.initialize(
        example.state, example.dag, example.policy, replan_context=example.context
    )

    summary = asyncio.run(executor.execute(example.state))

    assert [request.task_id for request in backend.requests] == ["task_c"]
    assert summary.executor_status is ExecutorStatus.PAUSED
    assert (
        store.load(example.state.run_id).pause_reason
        is RuntimePauseReason.BUDGET_EXHAUSTED
    )


def test_run_cancellation_preserves_timeout_as_attempt_terminal_cause() -> None:
    class TimeoutGate:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def sleep(self, milliseconds: int) -> None:
            del milliseconds
            self.started.set()
            await self.release.wait()

    async def scenario() -> None:
        example = _single_task(runtime_example(timeout_milliseconds=100))
        backend = MockTaskExecutionBackend(
            {
                ExecutionFixtureKey("task_a", 1): ExecutionFixture(
                    action=MockAction.NEVER_RETURN
                )
            }
        )
        timeout = TimeoutGate()
        executor, _, store, _, _ = _executor_parts(example, backend, timeout)
        executor.initialize(
            example.state, example.dag, example.policy, replan_context=example.context
        )
        execution = asyncio.create_task(executor.execute(example.state))
        await timeout.started.wait()
        executor.request_cancel()
        timeout.release.set()
        summary = await execution

        checkpoint = store.load(example.state.run_id)
        task = checkpoint.task_states[0]
        assert summary.executor_status is ExecutorStatus.CANCELLED
        assert task.status is TaskStatus.CANCELLED
        assert task.attempts[-1].status is AttemptStatus.TIMED_OUT
        event_types = [
            event.event_type for event in example.trace.read(example.state.run_id)
        ]
        assert TraceEventType.ATTEMPT_TIMED_OUT in event_types
        assert TraceEventType.ATTEMPT_CANCELLED not in event_types

    asyncio.run(scenario())
