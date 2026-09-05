"""Phase 10 workflow bridge contracts.

The handoff is deliberately separate from :mod:`researchos.domain.runtime`.
It is durable only until the ordinary Phase 3 checkpoint has been created.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from researchos.domain.claim_extraction import ClaimExtractionPolicy
from researchos.domain.contracts import (
    SCHEMA_VERSION,
    ContractModel,
    SafeId,
    Sha256,
    _require_aware,
    model_sha256,
)
from researchos.domain.evaluation import EvaluationPolicy
from researchos.domain.planning import PlanningPolicy, ReplanContext, TaskDAG
from researchos.domain.real_composition import ProviderSuboperationReservation
from researchos.domain.runtime import (
    ExecutionPolicy,
    RuntimeResourceAmount,
    TaskRuntimePolicy,
)
from researchos.domain.synthesis import VerificationPolicy

WORKFLOW_HANDOFF_SCHEMA_VERSION = 1


class WorkflowHandoffStatus(StrEnum):
    PREPARED = "prepared"
    CHECKPOINT_COMMITTED = "checkpoint_committed"


class WorkflowBudgetAllocation(ContractModel):
    """An immutable, exact component-wise partition of one Run budget."""

    total: RuntimeResourceAmount
    planning: RuntimeResourceAmount
    execution: RuntimeResourceAmount
    claim_extraction: RuntimeResourceAmount
    verification: RuntimeResourceAmount

    @model_validator(mode="after")
    def slices_exactly_partition_total(self) -> WorkflowBudgetAllocation:
        slices = (
            self.planning,
            self.execution,
            self.claim_extraction,
            self.verification,
        )
        for field in (
            "duration_milliseconds",
            "tokens",
            "cost_microunits",
            "tool_calls",
        ):
            if sum(getattr(item, field) for item in slices) != getattr(
                self.total, field
            ):
                raise ValueError("workflow budget slices must exactly partition total")
        return self


class Phase10ExecutionPolicyConfigV1(ContractModel):
    """Explicit Phase 10 scheduling/retry configuration, never a RunConfig edit."""

    schema_version: Literal[1] = 1
    max_concurrency: int = Field(ge=1)
    max_replans: int = Field(ge=0)
    task_policies: tuple[TaskRuntimePolicy, ...]


class Phase10WorkflowBudgetConfigV1(ContractModel):
    """Explicit, immutable Phase 10 allocation profile.

    The total is checked against the persisted Run effective budget at the
    application boundary.  Keeping every slice explicit avoids silently
    choosing production allocation ratios or rounding rules.
    """

    schema_version: Literal[1] = 1
    profile_id: SafeId
    profile_version: str = Field(min_length=1, max_length=80)
    allocation: WorkflowBudgetAllocation


class Phase10PlanningAdmission(ContractModel):
    """One exact allocation/reservation value frozen before initial planning.

    This is transient application input; its safe projection is persisted in
    the existing ``planning.started`` trace event rather than a new authority.
    """

    budget_config: Phase10WorkflowBudgetConfigV1
    workflow_profile_hash: Sha256
    mock_bundle_id: SafeId | None = None
    mock_bundle_version: str | None = None
    mock_bundle_hash: Sha256 | None = None
    allocation: WorkflowBudgetAllocation
    planning_reservation: ProviderSuboperationReservation | None = None

    @model_validator(mode="after")
    def admission_is_consistent(self) -> Phase10PlanningAdmission:
        if self.allocation != self.budget_config.allocation:
            raise ValueError("planning admission allocation differs from budget config")
        if self.planning_reservation is not None:
            reservation = RuntimeResourceAmount(
                duration_milliseconds=self.planning_reservation.duration_milliseconds,
                tokens=self.planning_reservation.tokens,
                cost_microunits=self.planning_reservation.cost_microunits,
                tool_calls=self.planning_reservation.tool_calls,
            )
            if not reservation.fits_within(self.allocation.planning):
                raise ValueError("planning reservation exceeds planning budget slice")
        return self

    @property
    def allocation_hash(self) -> Sha256:
        return model_sha256(self.allocation)

    @property
    def budget_config_hash(self) -> Sha256:
        return model_sha256(self.budget_config)

    def trace_attributes(self) -> dict[str, object]:
        """The frozen, secret-free projection for ``planning.started``."""

        return {
            "workflow_budget_allocation": self.allocation.model_dump(mode="json"),
            "workflow_budget_allocation_hash": self.allocation_hash,
            "planning_reservation": (
                self.planning_reservation.model_dump(mode="json")
                if self.planning_reservation is not None
                else None
            ),
            "planning_reservation_hash": (
                self.planning_reservation.reservation_hash
                if self.planning_reservation is not None
                else None
            ),
            "workflow_budget_profile_id": self.budget_config.profile_id,
            "workflow_budget_profile_version": self.budget_config.profile_version,
            "workflow_budget_config_hash": self.budget_config_hash,
            "workflow_profile_hash": self.workflow_profile_hash,
            "mock_bundle_id": self.mock_bundle_id,
            "mock_bundle_version": self.mock_bundle_version,
            "mock_bundle_hash": self.mock_bundle_hash,
        }


class Phase10EvaluationProfileV1(ContractModel):
    """The sole Phase 10 evaluation mode: deterministic structural self-check."""

    schema_version: Literal[1] = 1
    mode: Literal["structural_selfcheck_v1"] = "structural_selfcheck_v1"
    algorithm_version: Literal["phase10-structural-selfcheck-v1"] = (
        "phase10-structural-selfcheck-v1"
    )
    evaluation_policy: EvaluationPolicy

    @model_validator(mode="after")
    def selfcheck_is_offline_only(self) -> Phase10EvaluationProfileV1:
        if self.evaluation_policy.enable_model_evaluators:
            raise ValueError("Phase 10 structural self-check forbids model evaluators")
        if self.evaluation_policy.max_model_calls != 0:
            raise ValueError("Phase 10 structural self-check permits zero model calls")
        return self


class Phase10WorkflowProfileV1(ContractModel):
    """Explicit Phase 10 coordinator configuration, never durable authority."""

    schema_version: Literal[1] = 1
    profile_id: SafeId
    profile_version: str = Field(min_length=1, max_length=80)
    system_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    system_version: str = Field(min_length=1, max_length=80)
    budget: Phase10WorkflowBudgetConfigV1
    planning_policy: PlanningPolicy
    execution: Phase10ExecutionPolicyConfigV1
    claim_extraction_policy: ClaimExtractionPolicy
    verification_policy: VerificationPolicy
    evaluation: Phase10EvaluationProfileV1

    @property
    def profile_hash(self) -> Sha256:
        return model_sha256(self)


class WorkflowRuntimeHandoff(ContractModel):
    """Pinned planning-to-runtime bridge; never a task-state authority."""

    schema_version: Literal[WORKFLOW_HANDOFF_SCHEMA_VERSION] = (
        WORKFLOW_HANDOFF_SCHEMA_VERSION
    )
    handoff_id: SafeId
    handoff_revision: int = Field(default=0, ge=0)
    status: WorkflowHandoffStatus = WorkflowHandoffStatus.PREPARED
    run_id: SafeId
    planning_run_revision: int = Field(ge=0)
    runtime_run_revision: int = Field(ge=0)
    dag: TaskDAG
    dag_hash: Sha256
    execution_policy: ExecutionPolicy
    execution_policy_hash: Sha256
    workflow_profile_hash: Sha256
    mock_bundle_id: SafeId | None = None
    mock_bundle_version: str | None = None
    mock_bundle_hash: Sha256 | None = None
    replan_context: ReplanContext
    budget_allocation: WorkflowBudgetAllocation
    budget_allocation_hash: Sha256
    created_at: datetime
    updated_at: datetime
    semantic_hash: Sha256

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)

    @classmethod
    def build(
        cls,
        *,
        handoff_id: str,
        run_id: str,
        planning_run_revision: int,
        runtime_run_revision: int,
        dag: TaskDAG,
        execution_policy: ExecutionPolicy,
        replan_context: ReplanContext,
        budget_allocation: WorkflowBudgetAllocation,
        created_at: datetime,
        workflow_profile_hash: str,
        mock_bundle_id: str | None = None,
        mock_bundle_version: str | None = None,
        mock_bundle_hash: str | None = None,
    ) -> WorkflowRuntimeHandoff:
        payload = {
            "handoff_id": handoff_id,
            "run_id": run_id,
            "planning_run_revision": planning_run_revision,
            "runtime_run_revision": runtime_run_revision,
            "dag": dag,
            "execution_policy": execution_policy,
            "workflow_profile_hash": workflow_profile_hash,
            "mock_bundle_id": mock_bundle_id,
            "mock_bundle_version": mock_bundle_version,
            "mock_bundle_hash": mock_bundle_hash,
            "replan_context": replan_context,
            "budget_allocation": budget_allocation,
            "created_at": created_at,
        }
        return cls.model_validate(
            {
                **payload,
                "dag_hash": model_sha256(dag),
                "execution_policy_hash": model_sha256(execution_policy),
                "budget_allocation_hash": model_sha256(budget_allocation),
                "updated_at": created_at,
                "semantic_hash": model_sha256(_WorkflowHandoffSemantic(**payload)),
            }
        )

    @model_validator(mode="after")
    def pins_are_consistent(self) -> WorkflowRuntimeHandoff:
        if self.dag.run_id != self.run_id:
            raise ValueError("workflow handoff DAG belongs to another run")
        if self.dag.run_revision != self.planning_run_revision:
            raise ValueError("workflow handoff planning revision differs from DAG")
        if self.runtime_run_revision < self.planning_run_revision:
            raise ValueError("workflow handoff runtime revision precedes planning")
        if self.dag_hash != model_sha256(self.dag):
            raise ValueError("workflow handoff DAG hash differs")
        if self.execution_policy_hash != model_sha256(self.execution_policy):
            raise ValueError("workflow handoff execution policy hash differs")
        if self.budget_allocation_hash != model_sha256(self.budget_allocation):
            raise ValueError("workflow handoff allocation hash differs")
        semantic = _WorkflowHandoffSemantic(
            handoff_id=self.handoff_id,
            run_id=self.run_id,
            planning_run_revision=self.planning_run_revision,
            runtime_run_revision=self.runtime_run_revision,
            dag=self.dag,
            execution_policy=self.execution_policy,
            workflow_profile_hash=self.workflow_profile_hash,
            mock_bundle_id=self.mock_bundle_id,
            mock_bundle_version=self.mock_bundle_version,
            mock_bundle_hash=self.mock_bundle_hash,
            replan_context=self.replan_context,
            budget_allocation=self.budget_allocation,
            created_at=self.created_at,
        )
        if self.semantic_hash != model_sha256(semantic):
            raise ValueError("workflow handoff semantic hash differs")
        if self.updated_at < self.created_at:
            raise ValueError("workflow handoff updated_at precedes created_at")
        return self


class WorkflowRuntimeHandoffEnvelope(ContractModel):
    envelope_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    handoff: WorkflowRuntimeHandoff
    payload_sha256: Sha256

    @model_validator(mode="after")
    def payload_hash_matches(self) -> WorkflowRuntimeHandoffEnvelope:
        if self.payload_sha256 != model_sha256(self.handoff):
            raise ValueError("workflow handoff envelope hash differs")
        return self


class _WorkflowHandoffSemantic(ContractModel):
    handoff_id: SafeId
    run_id: SafeId
    planning_run_revision: int = Field(ge=0)
    runtime_run_revision: int = Field(ge=0)
    dag: TaskDAG
    execution_policy: ExecutionPolicy
    workflow_profile_hash: Sha256
    mock_bundle_id: SafeId | None = None
    mock_bundle_version: str | None = None
    mock_bundle_hash: Sha256 | None = None
    replan_context: ReplanContext
    budget_allocation: WorkflowBudgetAllocation
    created_at: datetime

    _aware_created = field_validator("created_at")(_require_aware)
