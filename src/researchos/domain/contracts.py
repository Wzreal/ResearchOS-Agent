"""Versioned Phase 1 domain contracts.

These models contain data and local invariants only. Time-relative lifecycle
rules, persistence, and operating-mode readiness belong to ``RunManager``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1

SafeId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ContractModel(BaseModel):
    """Shared strict and immutable model policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must be timezone-aware")
    return value


class OperatingMode(StrEnum):
    MOCK = "mock"
    REAL = "real"


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    VALIDATION = "validation"
    LIFECYCLE = "lifecycle"
    STORAGE = "storage"
    CORRUPTION = "corruption"
    COMPATIBILITY = "compatibility"
    INTERNAL = "internal"


class TraceEventType(StrEnum):
    TRANSITION_INTENT = "run.transition_intent"
    TRANSITION_COMMITTED = "run.transition_committed"
    TRANSITION_RECONCILED = "run.transition_reconciled"
    RESUMED = "run.resumed"
    PLANNING_STARTED = "planning.started"
    PLANNING_CANDIDATE_RECEIVED = "planning.candidate_received"
    PLANNING_MODEL_FAILED = "planning.model_failed"
    PLANNING_VALIDATION_FAILED = "planning.validation_failed"
    PLANNING_VALIDATED = "planning.validated"
    REPLAN_REQUESTED = "planning.replan_requested"
    REPLAN_DECISION = "planning.replan_decision"
    CHECKPOINT_INTENT = "runtime.checkpoint_intent"
    CHECKPOINT_COMMITTED = "runtime.checkpoint_committed"
    CHECKPOINT_RECONCILED = "runtime.checkpoint_reconciled"
    EXECUTOR_STARTED = "runtime.executor_started"
    EXECUTOR_PAUSED = "runtime.executor_paused"
    EXECUTOR_RESUMED = "runtime.executor_resumed"
    EXECUTOR_COMPLETED = "runtime.executor_completed"
    EXECUTOR_CANCEL_REQUESTED = "runtime.executor_cancel_requested"
    TASK_READY = "runtime.task_ready"
    TASK_STARTED = "runtime.task_started"
    TASK_SUCCEEDED = "runtime.task_succeeded"
    TASK_FAILED = "runtime.task_failed"
    TASK_BLOCKED = "runtime.task_blocked"
    TASK_CANCELLED = "runtime.task_cancelled"
    ATTEMPT_STARTED = "runtime.attempt_started"
    ATTEMPT_SUCCEEDED = "runtime.attempt_succeeded"
    ATTEMPT_FAILED = "runtime.attempt_failed"
    ATTEMPT_RETRY_SCHEDULED = "runtime.attempt_retry_scheduled"
    ATTEMPT_TIMED_OUT = "runtime.attempt_timed_out"
    ATTEMPT_CANCELLED = "runtime.attempt_cancelled"
    ATTEMPT_INTERRUPTED = "runtime.attempt_interrupted"
    BUDGET_RESERVED = "runtime.budget_reserved"
    BUDGET_COMMITTED = "runtime.budget_committed"
    BUDGET_RELEASED = "runtime.budget_released"
    BUDGET_BREACHED = "runtime.budget_breached"
    RUNTIME_REPLAN_REQUESTED = "runtime.replan_requested"
    RUNTIME_REPLAN_REJECTED = "runtime.replan_rejected"


class RunInput(ContractModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    query: Annotated[str, StringConstraints(min_length=1, max_length=100_000)]

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class BudgetLimits(ContractModel):
    """Effective limits, with deterministic defaults for omitted constraints."""

    max_duration_seconds: int = Field(default=3_600, gt=0)
    max_tokens: int = Field(default=100_000, gt=0)
    max_cost_microunits: int = Field(default=10_000_000, gt=0)
    cost_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    max_tool_calls: int = Field(default=100, gt=0)


class BudgetUsage(ContractModel):
    elapsed_milliseconds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_microunits: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


class Budget(ContractModel):
    limits: BudgetLimits
    usage: BudgetUsage = Field(default_factory=BudgetUsage)

    @model_validator(mode="after")
    def usage_must_not_exceed_limits(self) -> Budget:
        if self.usage.elapsed_milliseconds > self.limits.max_duration_seconds * 1_000:
            raise ValueError("elapsed time exceeds budget")
        if self.usage.tokens > self.limits.max_tokens:
            raise ValueError("tokens exceed budget")
        if self.usage.cost_microunits > self.limits.max_cost_microunits:
            raise ValueError("cost exceeds budget")
        if self.usage.tool_calls > self.limits.max_tool_calls:
            raise ValueError("tool calls exceed budget")
        return self


class RunConfig(ContractModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    mode: OperatingMode = OperatingMode.MOCK
    budget_limits: BudgetLimits = Field(default_factory=BudgetLimits)
    deadline: datetime | None = None
    allowed_capability_ids: tuple[SafeId, ...] = ()
    source_policy_id: SafeId | None = None
    output_format: Annotated[str, StringConstraints(min_length=1, max_length=80)] = (
        "markdown"
    )

    _aware_deadline = field_validator("deadline")(_require_aware)

    @field_validator("allowed_capability_ids")
    @classmethod
    def capability_ids_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed capability IDs must be unique")
        return value


class TaskRecord(ContractModel):
    """Minimal Phase 1 task identity, deliberately without DAG semantics."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    task_id: SafeId
    created_at: datetime
    updated_at: datetime

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> TaskRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        return self


class RunError(ContractModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    error_id: SafeId
    code: SafeId
    category: ErrorCategory
    message: Annotated[str, StringConstraints(min_length=1, max_length=4_000)]
    retryable: bool = False
    occurred_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)
    cause_error_id: SafeId | None = None

    _aware_occurred = field_validator("occurred_at")(_require_aware)

    @field_validator("details")
    @classmethod
    def details_must_be_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("error details must be finite JSON data") from exc
        return value


class ArtifactRecord(ContractModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    artifact_id: SafeId
    kind: SafeId
    relative_path: Annotated[str, StringConstraints(min_length=1, max_length=1_024)]
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    created_at: datetime
    producing_task_id: SafeId | None = None

    _aware_created = field_validator("created_at")(_require_aware)

    @field_validator("relative_path")
    @classmethod
    def path_must_stay_inside_run(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("artifact path must use POSIX separators")
        segments = value.split("/")
        posix_path = PurePosixPath(value)
        windows_path = PureWindowsPath(value)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part in {"", ".", ".."} for part in segments)
        ):
            raise ValueError("artifact path must be a safe relative path")
        return value


class RunState(ContractModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    run_id: SafeId
    revision: int = Field(ge=0)
    status: RunStatus
    input_snapshot: RunInput
    input_hash: Sha256
    input_redacted: bool = False
    config: RunConfig
    config_hash: Sha256
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    budget: Budget
    task_records: tuple[TaskRecord, ...] = ()
    artifacts: tuple[ArtifactRecord, ...] = ()
    errors: tuple[RunError, ...] = ()
    terminal_reason: str | None = None
    last_transition_id: SafeId

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)
    _aware_completed = field_validator("completed_at")(_require_aware)

    @model_validator(mode="after")
    def state_invariants_hold(self) -> RunState:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        terminal = self.status in TERMINAL_STATUSES
        if terminal != (self.completed_at is not None):
            raise ValueError("completed_at must be present exactly for terminal states")
        if self.status in {
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        } and not (self.terminal_reason and self.terminal_reason.strip()):
            raise ValueError("non-completed terminal states require a reason")
        if not terminal and self.terminal_reason is not None:
            raise ValueError("non-terminal states cannot have a terminal reason")
        if self.status is RunStatus.FAILED and not self.errors:
            raise ValueError("failed state requires at least one error")
        for records, attribute in (
            (self.task_records, "task_id"),
            (self.artifacts, "artifact_id"),
            (self.errors, "error_id"),
        ):
            identifiers = [getattr(record, attribute) for record in records]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"duplicate {attribute}")
        return self


class TraceEvent(ContractModel):
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

    _aware_timestamp = field_validator("timestamp")(_require_aware)

    @field_validator("attributes")
    @classmethod
    def attributes_must_be_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("trace attributes must be finite JSON data") from exc
        return value

    @model_validator(mode="after")
    def transition_fields_are_consistent(self) -> TraceEvent:
        transition_types = {
            TraceEventType.TRANSITION_INTENT,
            TraceEventType.TRANSITION_COMMITTED,
            TraceEventType.TRANSITION_RECONCILED,
        }
        if self.event_type in transition_types and self.transition_id is None:
            raise ValueError("transition event requires transition_id")
        if self.event_type in transition_types and self.next_status is None:
            raise ValueError("transition event requires next_status")
        return self


def canonical_json_bytes(model: BaseModel) -> bytes:
    """Serialize a domain model deterministically for hashing and storage."""

    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def model_sha256(model: BaseModel) -> str:
    """Hash the canonical persistent representation of a domain model."""

    return hashlib.sha256(canonical_json_bytes(model)).hexdigest()


def validate_sha256(value: str) -> bool:
    """Return whether a string has the persisted SHA-256 form."""

    return re.fullmatch(r"[0-9a-f]{64}", value) is not None
