"""Phase 3 durable DAG runtime contracts.

The checkpoint is a snapshot, not an event log.  Its one-mutation outbox only
contains immutable trace events needed to settle the most recent snapshot.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.contracts import (
    SCHEMA_VERSION,
    BudgetUsage,
    ContractModel,
    RunError,
    RunStatus,
    SafeId,
    Sha256,
    TraceEvent,
    TraceEventType,
    _require_aware,
    model_sha256,
)
from researchos.domain.planning import (
    ReplanContext,
    ReplanRequest,
    ResearchTask,
    TaskDAG,
)

RUNTIME_SCHEMA_VERSION = 1


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TASK_TERMINAL_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
)


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ExecutorStatus(StrEnum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DependencyPolicy(StrEnum):
    ALL_SUCCESS_REQUIRED = "all_success_required"


class IdempotencyMode(StrEnum):
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class UsageCertainty(StrEnum):
    EXACT = "exact"
    UPPER_BOUND = "upper_bound"
    UNKNOWN = "unknown"


class ExecutionResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RuntimePauseReason(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_BREACHED = "budget_breached"
    REPLAN_REQUESTED = "replan_requested"


class RuntimeResourceAmount(ContractModel):
    duration_milliseconds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_microunits: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)

    def plus(self, other: RuntimeResourceAmount) -> RuntimeResourceAmount:
        return RuntimeResourceAmount(
            duration_milliseconds=self.duration_milliseconds
            + other.duration_milliseconds,
            tokens=self.tokens + other.tokens,
            cost_microunits=self.cost_microunits + other.cost_microunits,
            tool_calls=self.tool_calls + other.tool_calls,
        )

    def minus(self, other: RuntimeResourceAmount) -> RuntimeResourceAmount:
        values = {
            "duration_milliseconds": self.duration_milliseconds
            - other.duration_milliseconds,
            "tokens": self.tokens - other.tokens,
            "cost_microunits": self.cost_microunits - other.cost_microunits,
            "tool_calls": self.tool_calls - other.tool_calls,
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("resource subtraction would be negative")
        return RuntimeResourceAmount(**values)

    def fits_within(
        self, other: RuntimeResourceAmount | RuntimeResourceBalance
    ) -> bool:
        return (
            self.duration_milliseconds <= other.duration_milliseconds
            and self.tokens <= other.tokens
            and self.cost_microunits <= other.cost_microunits
            and self.tool_calls <= other.tool_calls
        )


class RuntimeResourceBalance(ContractModel):
    """Signed availability; a breach is represented honestly, not clamped."""

    duration_milliseconds: int = 0
    tokens: int = 0
    cost_microunits: int = 0
    tool_calls: int = 0


class TaskRuntimePolicy(ContractModel):
    task_id: SafeId
    operation_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    dependency_policy: Literal[DependencyPolicy.ALL_SUCCESS_REQUIRED] = (
        DependencyPolicy.ALL_SUCCESS_REQUIRED
    )
    max_attempts: int = Field(default=1, ge=1)
    backoff_milliseconds: tuple[int, ...] = ()
    timeout_milliseconds: int = Field(gt=0)
    reservation: RuntimeResourceAmount
    idempotency: IdempotencyMode = IdempotencyMode.IDEMPOTENT
    retry_on_timeout: bool = False

    @field_validator("operation_version")
    @classmethod
    def operation_version_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operation_version must not be blank")
        return value

    @field_validator("backoff_milliseconds")
    @classmethod
    def backoff_must_be_non_negative(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(item < 0 for item in value):
            raise ValueError("backoff values must be non-negative")
        return value

    @model_validator(mode="after")
    def retry_policy_is_consistent(self) -> TaskRuntimePolicy:
        if len(self.backoff_milliseconds) != self.max_attempts - 1:
            raise ValueError("backoff schedule must contain max_attempts - 1 values")
        if (
            self.idempotency is IdempotencyMode.NON_IDEMPOTENT
            and self.max_attempts != 1
        ):
            raise ValueError(
                "non-idempotent operations cannot be automatically retried"
            )
        if self.reservation.duration_milliseconds != self.timeout_milliseconds:
            raise ValueError("duration reservation must equal the attempt timeout")
        return self


class ExecutionPolicy(ContractModel):
    schema_version: Literal[RUNTIME_SCHEMA_VERSION] = RUNTIME_SCHEMA_VERSION
    max_concurrency: int = Field(ge=1)
    tasks: tuple[TaskRuntimePolicy, ...]
    max_replans: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def task_policies_are_unique(self) -> ExecutionPolicy:
        ids = [item.task_id for item in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("execution policy contains duplicate task IDs")
        return self


class TaskAttempt(ContractModel):
    attempt_id: SafeId
    task_id: SafeId
    attempt_number: int = Field(ge=1)
    operation_key: Sha256
    attempt_key: Sha256
    status: AttemptStatus
    started_at: datetime
    finished_at: datetime | None = None
    usage: RuntimeResourceAmount | None = None
    usage_certainty: UsageCertainty = UsageCertainty.UNKNOWN
    failure_code: SafeId | None = None
    retryable: bool = False
    backend_receipt: SafeId | None = None

    _aware_started = field_validator("started_at")(_require_aware)
    _aware_finished = field_validator("finished_at")(_require_aware)

    @model_validator(mode="after")
    def attempt_fields_match_status(self) -> TaskAttempt:
        terminal = self.status is not AttemptStatus.RUNNING
        if terminal != (self.finished_at is not None):
            raise ValueError("finished_at is required exactly for settled attempts")
        if self.usage_certainty is not UsageCertainty.UNKNOWN and self.usage is None:
            raise ValueError("known usage certainty requires usage")
        if self.usage_certainty is UsageCertainty.UNKNOWN and self.usage is not None:
            raise ValueError("unknown usage cannot contain an amount")
        return self


class TaskOutcome(ContractModel):
    task_id: SafeId
    attempt_id: SafeId
    committed_at: datetime
    produced_output_ids: tuple[SafeId, ...] = ()
    backend_receipt: SafeId | None = None

    _aware_committed = field_validator("committed_at")(_require_aware)

    @field_validator("produced_output_ids")
    @classmethod
    def output_ids_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("outcome output IDs must be unique")
        return value


class TaskRuntimeState(ContractModel):
    task_id: SafeId
    status: TaskStatus = TaskStatus.PENDING
    attempts: tuple[TaskAttempt, ...] = ()
    outcome: TaskOutcome | None = None
    blocked_by: tuple[SafeId, ...] = ()
    next_eligible_at: datetime | None = None

    _aware_next = field_validator("next_eligible_at")(_require_aware)

    @model_validator(mode="after")
    def task_state_is_consistent(self) -> TaskRuntimeState:
        numbers = [item.attempt_number for item in self.attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("attempt numbers must be contiguous and start at one")
        if any(item.task_id != self.task_id for item in self.attempts):
            raise ValueError("attempt belongs to another task")
        operation_keys = {item.operation_key for item in self.attempts}
        if len(operation_keys) > 1:
            raise ValueError("task attempts must share one operation key")
        running_attempts = [
            item for item in self.attempts if item.status is AttemptStatus.RUNNING
        ]
        if self.status is TaskStatus.RUNNING:
            if len(running_attempts) != 1 or running_attempts[0] != self.attempts[-1]:
                raise ValueError("running task requires one latest running attempt")
        elif running_attempts:
            raise ValueError("non-running task cannot contain a running attempt")
        if self.status in {TaskStatus.PENDING, TaskStatus.BLOCKED} and self.attempts:
            raise ValueError("pending or blocked task cannot contain attempts")
        if self.status is TaskStatus.RETRY_WAIT and (
            not self.attempts
            or self.attempts[-1].status
            not in {
                AttemptStatus.FAILED,
                AttemptStatus.TIMED_OUT,
                AttemptStatus.INTERRUPTED,
            }
        ):
            raise ValueError("retry wait requires a retryable settled attempt")
        if self.status is TaskStatus.FAILED and (
            not self.attempts
            or self.attempts[-1].status
            not in {
                AttemptStatus.FAILED,
                AttemptStatus.TIMED_OUT,
                AttemptStatus.INTERRUPTED,
            }
        ):
            raise ValueError("failed task requires a failed settled attempt")
        if (self.status is TaskStatus.SUCCEEDED) != (self.outcome is not None):
            raise ValueError("only succeeded tasks have a committed outcome")
        if self.outcome is not None:
            matching = [
                item
                for item in self.attempts
                if item.attempt_id == self.outcome.attempt_id
            ]
            if (
                self.outcome.task_id != self.task_id
                or len(matching) != 1
                or matching[0].status is not AttemptStatus.SUCCEEDED
            ):
                raise ValueError("task outcome must reference its succeeded attempt")
        if self.status is TaskStatus.BLOCKED and not self.blocked_by:
            raise ValueError("blocked task requires root blockers")
        if self.status is not TaskStatus.BLOCKED and self.blocked_by:
            raise ValueError("only blocked tasks contain blockers")
        if self.status is TaskStatus.RETRY_WAIT and self.next_eligible_at is None:
            raise ValueError("retry wait requires next eligible time")
        if (
            self.status is not TaskStatus.RETRY_WAIT
            and self.next_eligible_at is not None
        ):
            raise ValueError("only retry wait contains next eligible time")
        return self


class RuntimeBudgetState(ContractModel):
    limits: RuntimeResourceAmount
    consumed: RuntimeResourceAmount = Field(default_factory=RuntimeResourceAmount)
    reserved: RuntimeResourceAmount = Field(default_factory=RuntimeResourceAmount)
    uncertain_consumption: RuntimeResourceAmount = Field(
        default_factory=RuntimeResourceAmount
    )
    breached: bool = False

    def available(self) -> RuntimeResourceBalance:
        def remaining(limit: int, consumed: int, reserved: int, uncertain: int) -> int:
            return limit - consumed - reserved - uncertain

        return RuntimeResourceBalance(
            duration_milliseconds=remaining(
                self.limits.duration_milliseconds,
                self.consumed.duration_milliseconds,
                self.reserved.duration_milliseconds,
                self.uncertain_consumption.duration_milliseconds,
            ),
            tokens=remaining(
                self.limits.tokens,
                self.consumed.tokens,
                self.reserved.tokens,
                self.uncertain_consumption.tokens,
            ),
            cost_microunits=remaining(
                self.limits.cost_microunits,
                self.consumed.cost_microunits,
                self.reserved.cost_microunits,
                self.uncertain_consumption.cost_microunits,
            ),
            tool_calls=remaining(
                self.limits.tool_calls,
                self.consumed.tool_calls,
                self.reserved.tool_calls,
                self.uncertain_consumption.tool_calls,
            ),
        )

    @model_validator(mode="after")
    def negative_availability_requires_breach(self) -> RuntimeBudgetState:
        available = self.available()
        if not self.breached and any(
            value < 0
            for value in (
                available.duration_milliseconds,
                available.tokens,
                available.cost_microunits,
                available.tool_calls,
            )
        ):
            raise ValueError("negative budget availability requires breached=true")
        return self


class DurableReplanState(ContractModel):
    context: ReplanContext
    completed_replans: int = Field(default=0, ge=0)
    slots_used: int = Field(default=0, ge=0)
    max_replans: int = Field(ge=0)
    pending_request: ReplanRequest | None = None

    @model_validator(mode="after")
    def accounting_is_bounded(self) -> DurableReplanState:
        if (
            self.completed_replans > self.slots_used
            or self.slots_used > self.max_replans
        ):
            raise ValueError("durable replan accounting exceeds its bounds")
        outstanding = self.slots_used - self.completed_replans
        if outstanding not in {0, 1}:
            raise ValueError("durable replan has an invalid outstanding slot count")
        if (self.pending_request is not None) != (outstanding == 1):
            raise ValueError("pending replan must own exactly one reserved slot")
        if (
            self.pending_request is not None
            and self.pending_request.context != self.context
        ):
            raise ValueError("pending replan context differs from durable lineage")
        return self


class TraceEventDescriptor(ContractModel):
    """Complete immutable event bytes in model form for exact replay."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    event_id: SafeId
    event_type: TraceEventType
    timestamp: datetime
    run_id: SafeId
    transition_id: SafeId | None = None
    revision: int = Field(ge=0)
    previous_status: RunStatus | None = None
    next_status: RunStatus | None = None
    correlation_id: SafeId | None = None
    causation_id: SafeId | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    budget_delta: BudgetUsage | None = None
    error: RunError | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    canonical_event_hash: Sha256

    _aware_timestamp = field_validator("timestamp")(_require_aware)

    @classmethod
    def from_event(cls, event: TraceEvent) -> TraceEventDescriptor:
        return cls(
            **event.model_dump(mode="python"), canonical_event_hash=model_sha256(event)
        )

    def to_event(self) -> TraceEvent:
        raw = self.model_dump(mode="python", exclude={"canonical_event_hash"})
        return TraceEvent.model_validate(raw)

    @model_validator(mode="after")
    def hash_matches_exact_event(self) -> TraceEventDescriptor:
        if model_sha256(self.to_event()) != self.canonical_event_hash:
            raise ValueError("trace descriptor hash does not match its event")
        return self


class CheckpointMutation(ContractModel):
    mutation_id: SafeId
    kind: SafeId
    events: tuple[TraceEventDescriptor, ...]

    @model_validator(mode="after")
    def event_ids_are_unique(self) -> CheckpointMutation:
        ids = [item.event_id for item in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("checkpoint outbox contains duplicate event IDs")
        return self


class RuntimeCheckpoint(ContractModel):
    schema_version: Literal[RUNTIME_SCHEMA_VERSION] = RUNTIME_SCHEMA_VERSION
    checkpoint_revision: int = Field(ge=0)
    run_id: SafeId
    run_revision: int = Field(ge=0)
    dag_id: SafeId
    plan_id: SafeId
    dag: TaskDAG
    dag_hash: Sha256
    execution_policy: ExecutionPolicy
    execution_policy_hash: Sha256
    executor_status: ExecutorStatus
    pause_reason: RuntimePauseReason | None = None
    task_states: tuple[TaskRuntimeState, ...]
    budget: RuntimeBudgetState
    replan: DurableReplanState
    last_mutation: CheckpointMutation
    created_at: datetime
    updated_at: datetime
    cancellation_requested_at: datetime | None = None

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)
    _aware_cancel = field_validator("cancellation_requested_at")(_require_aware)

    @model_validator(mode="after")
    def checkpoint_is_self_consistent(self) -> RuntimeCheckpoint:
        if self.dag.run_id != self.run_id or self.dag.dag_id != self.dag_id:
            raise ValueError("checkpoint and DAG identity differ")
        if self.dag.plan_id != self.plan_id or model_sha256(self.dag) != self.dag_hash:
            raise ValueError("checkpoint DAG hash or plan identity differs")
        if self.dag.run_revision > self.run_revision:
            raise ValueError("checkpoint DAG revision is newer than execution run")
        if model_sha256(self.execution_policy) != self.execution_policy_hash:
            raise ValueError("checkpoint execution policy hash differs")
        if (
            self.replan.context.prior_plan_id != self.plan_id
            or self.replan.max_replans != self.execution_policy.max_replans
            or (
                self.replan.pending_request is not None
                and self.replan.pending_request.run_id != self.run_id
            )
        ):
            raise ValueError("checkpoint replan identity or policy differs")
        dag_tasks = {item.task_id for item in self.dag.tasks}
        policy_tasks = {item.task_id for item in self.execution_policy.tasks}
        runtime_tasks = [item.task_id for item in self.task_states]
        if dag_tasks != policy_tasks or dag_tasks != set(runtime_tasks):
            raise ValueError("DAG, policy, and runtime task sets must match")
        if len(runtime_tasks) != len(set(runtime_tasks)):
            raise ValueError("checkpoint contains duplicate task states")
        attempts = [
            attempt for task in self.task_states for attempt in task.attempts
        ]
        for field in ("attempt_id", "attempt_key"):
            values = [getattr(attempt, field) for attempt in attempts]
            if len(values) != len(set(values)):
                raise ValueError(f"checkpoint contains duplicate {field}")
        expected_outputs = {
            task.task_id: {output.output_id for output in task.expected_outputs}
            for task in self.dag.tasks
        }
        if any(
            task.outcome is not None
            and set(task.outcome.produced_output_ids)
            != expected_outputs[task.task_id]
            for task in self.task_states
        ):
            raise ValueError("task outcome does not match declared output IDs")
        if any(
            descriptor.run_id != self.run_id
            or descriptor.revision != self.run_revision
            or descriptor.correlation_id != self.last_mutation.mutation_id
            for descriptor in self.last_mutation.events
        ):
            raise ValueError("checkpoint outbox event identity differs")
        if self.updated_at < self.created_at:
            raise ValueError("checkpoint updated_at precedes created_at")
        paused = self.executor_status is ExecutorStatus.PAUSED
        if paused != (self.pause_reason is not None):
            raise ValueError("pause reason is required exactly when paused")
        if self.executor_status is ExecutorStatus.COMPLETED and not all(
            state.status in TASK_TERMINAL_STATUSES for state in self.task_states
        ):
            raise ValueError("completed executor requires all tasks terminal")
        return self


class CheckpointEnvelope(ContractModel):
    envelope_version: Literal[1] = 1
    checkpoint: RuntimeCheckpoint
    payload_sha256: Sha256

    @model_validator(mode="after")
    def payload_hash_matches(self) -> CheckpointEnvelope:
        if model_sha256(self.checkpoint) != self.payload_sha256:
            raise ValueError("checkpoint envelope payload hash differs")
        return self


class TaskExecutionRequest(ContractModel):
    run_id: SafeId
    run_revision: int = Field(ge=0)
    dag_id: SafeId
    dag_hash: Sha256
    task: ResearchTask
    task_id: SafeId
    attempt_id: SafeId
    attempt_number: int = Field(ge=1)
    operation_key: Sha256
    attempt_key: Sha256
    operation_version: str
    deadline: datetime
    hard_limits: RuntimeResourceAmount
    prior_backend_receipt: SafeId | None = None

    _aware_deadline = field_validator("deadline")(_require_aware)

    @model_validator(mode="after")
    def task_identity_matches(self) -> TaskExecutionRequest:
        if self.task.task_id != self.task_id:
            raise ValueError("execution request task identity differs")
        return self


class TaskExecutionResult(ContractModel):
    status: ExecutionResultStatus
    usage: RuntimeResourceAmount | None = None
    usage_certainty: UsageCertainty = UsageCertainty.UNKNOWN
    retryable: bool = False
    failure_code: SafeId | None = None
    backend_receipt: SafeId | None = None
    produced_output_ids: tuple[SafeId, ...] = ()

    @field_validator("produced_output_ids")
    @classmethod
    def output_ids_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("produced output IDs must be unique")
        return value

    @model_validator(mode="after")
    def result_is_consistent(self) -> TaskExecutionResult:
        if self.usage_certainty is UsageCertainty.UNKNOWN and self.usage is not None:
            raise ValueError("unknown usage cannot contain an amount")
        if self.usage_certainty is not UsageCertainty.UNKNOWN and self.usage is None:
            raise ValueError("known usage certainty requires an amount")
        if self.status is ExecutionResultStatus.SUCCEEDED and self.failure_code:
            raise ValueError("successful execution cannot have a failure code")
        if self.status is ExecutionResultStatus.FAILED and not self.failure_code:
            raise ValueError("failed execution requires a failure code")
        return self


class ExecutionSummary(ContractModel):
    run_id: SafeId
    checkpoint_revision: int = Field(ge=0)
    executor_status: ExecutorStatus
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    cancelled: int = Field(ge=0)
