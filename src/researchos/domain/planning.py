"""Phase 2 provider-independent planning and validated DAG contracts."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.contracts import ContractModel, SafeId, _require_aware
from researchos.domain.provider_diagnostics import validate_provider_diagnostics

PLANNING_SCHEMA_VERSION = 1
VALIDATOR_VERSION = "dag-validator-v1"

NonBlank = Annotated[str, StringConstraints(min_length=1, max_length=4_000)]


class CandidatePerspective(ContractModel):
    """Structurally parsed perspective before semantic validation."""

    perspective_id: str
    name: str
    title: str
    goal: str
    rationale: str
    priority: int = 50


class Perspective(ContractModel):
    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    perspective_id: SafeId
    name: NonBlank
    title: NonBlank
    goal: NonBlank
    rationale: NonBlank
    priority: int = Field(default=50, ge=1, le=100)

    @field_validator("name", "title", "goal", "rationale")
    @classmethod
    def descriptive_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("perspective descriptive field must not be blank")
        return value


class CandidateDependency(ContractModel):
    task_id: str


class TaskDependency(ContractModel):
    task_id: SafeId


class CandidateExpectedOutput(ContractModel):
    output_id: str
    description: str
    media_type: str


class ExpectedOutput(ContractModel):
    output_id: SafeId
    description: NonBlank
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]

    @field_validator("description", "media_type")
    @classmethod
    def output_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("expected output field must not be blank")
        return value


class ResourceEstimate(ContractModel):
    duration_milliseconds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_microunits: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)

    def plus(self, other: ResourceEstimate) -> ResourceEstimate:
        return ResourceEstimate(
            duration_milliseconds=(
                self.duration_milliseconds + other.duration_milliseconds
            ),
            tokens=self.tokens + other.tokens,
            cost_microunits=self.cost_microunits + other.cost_microunits,
            tool_calls=self.tool_calls + other.tool_calls,
        )


class TaskPolicy(ContractModel):
    priority: int = Field(default=50, ge=1, le=100)
    required: bool = True


class CandidateResearchTask(ContractModel):
    task_id: str
    perspective_id: str
    objective: str
    dependencies: tuple[CandidateDependency, ...] = ()
    required_capability_ids: tuple[str, ...] = ()
    expected_outputs: tuple[CandidateExpectedOutput, ...] = ()
    estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)
    policy: TaskPolicy = Field(default_factory=TaskPolicy)


class ResearchTask(ContractModel):
    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    task_id: SafeId
    perspective_id: SafeId
    objective: NonBlank
    dependencies: tuple[TaskDependency, ...] = ()
    required_capability_ids: tuple[SafeId, ...] = ()
    expected_outputs: tuple[ExpectedOutput, ...]
    estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)
    policy: TaskPolicy = Field(default_factory=TaskPolicy)

    @field_validator("objective")
    @classmethod
    def objective_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task objective must not be blank")
        return value

    @model_validator(mode="after")
    def output_ids_must_be_unique(self) -> ResearchTask:
        output_ids = [output.output_id for output in self.expected_outputs]
        if len(output_ids) != len(set(output_ids)):
            raise ValueError("expected output IDs must be unique within a task")
        return self


class PlanningPolicy(ContractModel):
    max_tasks: int = Field(gt=0)
    max_graph_depth: int = Field(gt=0)
    max_dependencies_per_task: int = Field(ge=0)


class PlannerMetadata(ContractModel):
    metadata_version: Literal[1] = 1
    planning_model_id: SafeId
    planner_id: SafeId
    planner_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]


class CandidatePlan(ContractModel):
    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    run_id: str
    plan_id: str
    perspectives: tuple[CandidatePerspective, ...]
    tasks: tuple[CandidateResearchTask, ...]
    planner_metadata: PlannerMetadata


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssueCode(StrEnum):
    CANDIDATE_MALFORMED = "candidate_malformed"
    REQUEST_RUN_MISMATCH = "request_run_mismatch"
    REQUEST_PLAN_MISMATCH = "request_plan_mismatch"
    PLANNER_PROVENANCE_MISMATCH = "planner_provenance_mismatch"
    DUPLICATE_PERSPECTIVE_ID = "duplicate_perspective_id"
    DUPLICATE_TASK_ID = "duplicate_task_id"
    INVALID_PERSPECTIVE = "invalid_perspective"
    UNKNOWN_PERSPECTIVE = "unknown_perspective"
    ORPHAN_PERSPECTIVE = "orphan_perspective"
    INVALID_TASK_ID = "invalid_task_id"
    EMPTY_OBJECTIVE = "empty_objective"
    DUPLICATE_DEPENDENCY = "duplicate_dependency"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    SELF_DEPENDENCY = "self_dependency"
    CYCLE_DETECTED = "cycle_detected"
    INVALID_EXPECTED_OUTPUT = "invalid_expected_output"
    DUPLICATE_EXPECTED_OUTPUT = "duplicate_expected_output"
    DUPLICATE_CAPABILITY = "duplicate_capability"
    INVALID_CAPABILITY_ID = "invalid_capability_id"
    UNAUTHORIZED_CAPABILITY = "unauthorized_capability"
    TASK_LIMIT_EXCEEDED = "task_limit_exceeded"
    DEPENDENCY_LIMIT_EXCEEDED = "dependency_limit_exceeded"
    DEPTH_LIMIT_EXCEEDED = "depth_limit_exceeded"
    BUDGET_DURATION_EXCEEDED = "budget_duration_exceeded"
    BUDGET_TOKENS_EXCEEDED = "budget_tokens_exceeded"
    BUDGET_COST_EXCEEDED = "budget_cost_exceeded"
    BUDGET_TOOL_CALLS_EXCEEDED = "budget_tool_calls_exceeded"


class ValidationIssue(ContractModel):
    code: ValidationIssueCode
    severity: ValidationSeverity = ValidationSeverity.ERROR
    message: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    task_id: str | None = None
    perspective_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def details_must_be_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("validation issue details must be finite JSON") from exc
        return value


class ValidationResult(ContractModel):
    validator_version: Literal[VALIDATOR_VERSION] = VALIDATOR_VERSION
    valid: bool
    issues: tuple[ValidationIssue, ...]
    topological_order: tuple[str, ...] = ()
    graph_depth: int | None = Field(default=None, ge=0)
    total_estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)
    critical_path_duration_milliseconds: int | None = Field(default=None, ge=0)
    perspective_count: int = Field(default=0, ge=0)
    task_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validity_matches_issues(self) -> ValidationResult:
        has_errors = any(
            issue.severity is ValidationSeverity.ERROR for issue in self.issues
        )
        if self.valid != (not has_errors):
            raise ValueError("valid must be true exactly when there are no errors")
        metrics_available = self.graph_depth is not None
        if metrics_available != (
            self.critical_path_duration_milliseconds is not None
        ):
            raise ValueError("graph metrics must be available or absent together")
        if not metrics_available and self.topological_order:
            raise ValueError("unsafe graph cannot expose topological order")
        return self


class ValidationSummary(ContractModel):
    validator_version: Literal[VALIDATOR_VERSION] = VALIDATOR_VERSION
    graph_depth: int = Field(ge=0)
    total_estimate: ResourceEstimate
    critical_path_duration_milliseconds: int = Field(ge=0)
    perspective_count: int = Field(ge=0)
    task_count: int = Field(ge=0)


class TaskDAG(ContractModel):
    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    dag_id: SafeId
    plan_id: SafeId
    run_id: SafeId
    run_revision: int = Field(ge=0)
    planning_request_id: SafeId
    perspectives: tuple[Perspective, ...]
    tasks: tuple[ResearchTask, ...]
    topological_order: tuple[SafeId, ...]
    planner_metadata: PlannerMetadata
    validation_summary: ValidationSummary
    created_at: datetime

    _aware_created = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def strict_graph_invariants_hold(self) -> TaskDAG:
        perspective_ids = [item.perspective_id for item in self.perspectives]
        task_ids = [item.task_id for item in self.tasks]
        if len(perspective_ids) != len(set(perspective_ids)):
            raise ValueError("validated DAG contains duplicate perspective IDs")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("validated DAG contains duplicate task IDs")
        if len(self.topological_order) != len(set(self.topological_order)) or set(
            self.topological_order
        ) != set(task_ids):
            raise ValueError("topological order must contain every task exactly once")
        positions = {
            task_id: position
            for position, task_id in enumerate(self.topological_order)
        }
        known_perspectives = set(perspective_ids)
        known_tasks = set(task_ids)
        for task in self.tasks:
            if task.perspective_id not in known_perspectives:
                raise ValueError("validated task references unknown perspective")
            dependencies = [item.task_id for item in task.dependencies]
            if len(dependencies) != len(set(dependencies)):
                raise ValueError("validated task contains duplicate dependencies")
            if any(
                dependency not in known_tasks
                or dependency == task.task_id
                or positions[dependency] >= positions[task.task_id]
                for dependency in dependencies
            ):
                raise ValueError("validated task dependency violates topological order")
        return self


class RemainingBudget(ContractModel):
    duration_milliseconds: int = Field(ge=0)
    tokens: int = Field(ge=0)
    cost_microunits: int = Field(ge=0)
    tool_calls: int = Field(ge=0)


class ReplanContext(ContractModel):
    """In-memory lineage cursor for bounded replanning in one service flow."""

    prior_plan_id: SafeId
    replan_count: int = Field(ge=0)
    prior_decision_id: SafeId | None = None


class PlanningRequest(ContractModel):
    schema_version: Literal[PLANNING_SCHEMA_VERSION] = PLANNING_SCHEMA_VERSION
    request_id: SafeId
    run_id: SafeId
    run_revision: int = Field(ge=0)
    plan_id: SafeId
    query: NonBlank
    source_policy_id: SafeId | None = None
    output_format: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    requested_at: datetime
    allowed_capability_ids: tuple[SafeId, ...]
    remaining_budget: RemainingBudget
    policy: PlanningPolicy
    replan_count: int = Field(default=0, ge=0)
    reason_code: SafeId | None = None
    reason: Annotated[str, StringConstraints(min_length=1, max_length=1_000)] | None = (
        None
    )

    _aware_requested = field_validator("requested_at")(_require_aware)

    @model_validator(mode="after")
    def replan_fields_are_consistent(self) -> PlanningRequest:
        if self.replan_count == 0:
            if self.reason_code is not None or self.reason is not None:
                raise ValueError("initial planning cannot contain replan reason fields")
        elif self.reason_code is None or self.reason is None:
            raise ValueError("reason fields are required for replans")
        return self


class PlanningModelResponse(ContractModel):
    response_version: Literal[1] = 1
    planning_model_id: SafeId
    payload: dict[str, Any]
    provider_diagnostics: dict[str, Any] | None = Field(default=None, exclude=True)

    @field_validator("payload")
    @classmethod
    def payload_must_be_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("planning payload must be finite JSON") from exc
        return value

    @field_validator("provider_diagnostics")
    @classmethod
    def provider_diagnostics_must_be_json(cls, value: dict[str, Any] | None):
        return validate_provider_diagnostics(value)


class PlanningStatus(StrEnum):
    VALIDATED = "validated"
    INVALID = "invalid"
    MALFORMED = "malformed"
    MODEL_ERROR = "model_error"


class PlanningError(ContractModel):
    code: SafeId
    message: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    retryable: bool = False


class PlanningResult(ContractModel):
    status: PlanningStatus
    run_id: SafeId
    run_revision: int = Field(ge=0)
    plan_id: SafeId
    planning_request_id: SafeId
    validation: ValidationResult | None
    validated_dag: TaskDAG | None = None
    planning_error: PlanningError | None = None
    replan_context: ReplanContext

    @model_validator(mode="after")
    def status_invariants_hold(self) -> PlanningResult:
        if self.replan_context.prior_plan_id != self.plan_id:
            raise ValueError("replan context must identify this result plan")
        if self.status is PlanningStatus.VALIDATED:
            if (
                self.validation is None
                or not self.validation.valid
                or self.validation.graph_depth is None
                or self.validation.critical_path_duration_milliseconds is None
                or self.validated_dag is None
                or self.planning_error is not None
            ):
                raise ValueError("invalid VALIDATED planning result")
            if (
                self.validated_dag.run_id != self.run_id
                or self.validated_dag.run_revision != self.run_revision
                or self.validated_dag.plan_id != self.plan_id
                or self.validated_dag.planning_request_id
                != self.planning_request_id
            ):
                raise ValueError("validated DAG identity differs from planning result")
        elif self.status is PlanningStatus.INVALID:
            if (
                self.validation is None
                or self.validation.valid
                or self.validated_dag is not None
                or self.planning_error is not None
            ):
                raise ValueError("invalid INVALID planning result")
        elif self.status is PlanningStatus.MALFORMED:
            malformed = self.validation is not None and any(
                issue.code is ValidationIssueCode.CANDIDATE_MALFORMED
                for issue in self.validation.issues
            )
            if (
                not malformed
                or self.validation.valid
                or self.validated_dag is not None
                or self.planning_error is not None
            ):
                raise ValueError("invalid MALFORMED planning result")
        elif (
            self.validation is not None
            or self.planning_error is None
            or self.validated_dag is not None
        ):
            raise ValueError("invalid MODEL_ERROR planning result")
        return self


class ReplanPolicy(ContractModel):
    max_replans: int = Field(ge=0)


class ReplanRequest(ContractModel):
    request_id: SafeId
    run_id: SafeId
    context: ReplanContext
    reason_code: SafeId
    reason: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]


class ReplanDecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED_LIMIT = "rejected_limit"
    REJECTED_CONTEXT = "rejected_context"


class ReplanDecision(ContractModel):
    decision_id: SafeId
    request_id: SafeId
    run_id: SafeId
    prior_plan_id: SafeId
    prior_decision_id: SafeId | None
    current_replan_count: int = Field(ge=0)
    next_replan_count: int | None = Field(default=None, ge=1)
    status: ReplanDecisionStatus
    reason_code: SafeId

    @model_validator(mode="after")
    def decision_count_is_consistent(self) -> ReplanDecision:
        if self.status is ReplanDecisionStatus.APPROVED:
            if self.next_replan_count != self.current_replan_count + 1:
                raise ValueError("approved decision must increment replan count")
        elif self.next_replan_count is not None:
            raise ValueError("rejected decision cannot have next replan count")
        return self


class ReplanResult(ContractModel):
    decision: ReplanDecision
    planning_result: PlanningResult | None = None

    @model_validator(mode="after")
    def approval_matches_result(self) -> ReplanResult:
        approved = self.decision.status is ReplanDecisionStatus.APPROVED
        if approved != (self.planning_result is not None):
            raise ValueError("approved decision requires exactly one planning result")
        return self
