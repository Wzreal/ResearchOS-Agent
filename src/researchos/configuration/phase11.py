"""Explicit, non-secret Phase 11 REAL benchmark configuration."""

from __future__ import annotations

from pydantic import Field, model_validator

from researchos.configuration.real_settings import RealIntegrationSettings
from researchos.domain.contracts import (
    BudgetLimits,
    ContractModel,
    OperatingMode,
    RunConfig,
    SafeId,
    Sha256,
    model_sha256,
)
from researchos.domain.evaluation import EvaluationDataset, EvaluationPolicy
from researchos.domain.identity import stable_hash
from researchos.domain.workflow import Phase10WorkflowProfileV1

PHASE11_HARD_COST_CEILING_MICROUNITS = 50_000_000
PHASE11_SECONDARY_REVIEW_CASE_IDS_V1 = (
    "p11_citation_chain",
    "p11_comparison_primary",
    "p11_conflict_multistep",
    "p11_evidence_synthesis",
    "p11_multisource_factual",
    "p11_time_bounded_public",
)


def phase11_secondary_review_subset_hash() -> str:
    return stable_hash(PHASE11_SECONDARY_REVIEW_CASE_IDS_V1)


class Phase11RealBenchmarkBundleV1(ContractModel):
    """Configuration only; credentials and runtime adapters stay external."""

    bundle_id: SafeId
    bundle_version: str = Field(min_length=1, max_length=80)
    workflow_profile: Phase10WorkflowProfileV1
    benchmark: EvaluationDataset
    evaluation_policy: EvaluationPolicy
    real_settings_hash: Sha256
    capability_ids: tuple[SafeId, ...]
    approved_refs: tuple[str, ...]
    max_agent_steps: int = Field(ge=1, le=100)
    max_agent_tool_calls: int = Field(ge=0, le=1_000)
    system_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    system_version: str = Field(min_length=1, max_length=80)
    max_batch_cost_microunits: int = Field(
        gt=0, le=PHASE11_HARD_COST_CEILING_MICROUNITS
    )

    @model_validator(mode="after")
    def pins_are_consistent(self) -> Phase11RealBenchmarkBundleV1:
        if self.workflow_profile.system_commit_sha != self.system_commit_sha:
            raise ValueError("workflow profile commit differs from benchmark bundle")
        if self.workflow_profile.system_version != self.system_version:
            raise ValueError("workflow profile version differs from benchmark bundle")
        if not {"reference_exact", "phase11_operational"}.issubset(
            self.evaluation_policy.enabled_evaluator_ids
        ):
            raise ValueError("Phase 11 policy requires ExactReferenceEvaluator")
        if tuple(sorted(self.capability_ids)) != self.capability_ids:
            raise ValueError("capability IDs must be sorted")
        if len(self.capability_ids) != len(set(self.capability_ids)):
            raise ValueError("capability IDs must be unique")
        if (
            not self.approved_refs
            or tuple(sorted(self.approved_refs)) != self.approved_refs
        ):
            raise ValueError("approved refs must be a non-empty sorted tuple")
        return self

    @property
    def bundle_hash(self) -> Sha256:
        return model_sha256(self)


class Phase11PaidAdmission(ContractModel):
    """Transient CLI admission input; never a second budget authority."""

    expected_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    approved_cost_microunits: int = Field(gt=0, le=PHASE11_HARD_COST_CEILING_MICROUNITS)
    real_acknowledged: bool

    @model_validator(mode="after")
    def acknowledgement_is_explicit(self) -> Phase11PaidAdmission:
        if not self.real_acknowledged:
            raise ValueError("explicit REAL acknowledgement is required")
        return self


def require_paid_admission(
    admission: Phase11PaidAdmission,
    *,
    current_commit_sha: str,
    is_clean: bool,
    approved_ref: bool,
    next_reservation_microunits: int,
    consumed_or_unknown_microunits: int = 0,
) -> int:
    """Fail closed before dispatch and return remaining approved capacity."""

    if not is_clean:
        raise ValueError("REAL benchmark requires a clean working tree")
    if current_commit_sha != admission.expected_commit_sha:
        raise ValueError("REAL benchmark commit SHA differs from expected SHA")
    if not approved_ref:
        raise ValueError("REAL benchmark ref is not approved")
    remaining = admission.approved_cost_microunits - consumed_or_unknown_microunits
    if next_reservation_microunits < 0 or next_reservation_microunits > remaining:
        raise ValueError("REAL benchmark reservation exceeds approved capacity")
    return remaining


def phase11_evaluation_policy(profile: Phase10WorkflowProfileV1) -> EvaluationPolicy:
    """Add the Phase 7 reference evaluator to the Phase 11 evaluation policy."""

    source = profile.evaluation.evaluation_policy
    return EvaluationPolicy.model_validate(
        {
            **source.model_dump(mode="python"),
            "policy_id": "phase11_real_evaluation",
            "policy_version": "1",
            "enabled_evaluator_ids": tuple(
                sorted(
                    (
                        *source.enabled_evaluator_ids,
                        "reference_exact",
                        "phase11_operational",
                    )
                )
            ),
        }
    )


class Phase11BatchCapacity:
    """Transient operator-process capacity; deliberately never persisted."""

    def __init__(self, approved_cost_microunits: int) -> None:
        if not 0 < approved_cost_microunits <= PHASE11_HARD_COST_CEILING_MICROUNITS:
            raise ValueError("batch approved cost is outside the hard ceiling")
        self._remaining = approved_cost_microunits

    @property
    def remaining_microunits(self) -> int:
        return self._remaining

    def reserve(self, upper_bound_microunits: int) -> None:
        if upper_bound_microunits < 0 or upper_bound_microunits > self._remaining:
            raise ValueError("REAL benchmark reservation exceeds batch capacity")
        self._remaining -= upper_bound_microunits


def phase11_real_run_config(
    bundle: Phase11RealBenchmarkBundleV1,
    settings: RealIntegrationSettings,
) -> RunConfig:
    """Derive the existing RunConfig v1 from explicit pinned configuration."""

    if model_sha256(settings) != bundle.real_settings_hash:
        raise ValueError("Phase 11 REAL settings differ from bundle pin")
    capability_ids = tuple(item.capability_id for item in settings.capability_settings)
    if capability_ids != bundle.capability_ids:
        raise ValueError("Phase 11 REAL capabilities differ from bundle pin")
    total = bundle.workflow_profile.budget.allocation.total
    return RunConfig(
        mode=OperatingMode.REAL,
        budget_limits=BudgetLimits(
            max_duration_seconds=max(1, total.duration_milliseconds // 1_000),
            max_tokens=total.tokens,
            max_cost_microunits=total.cost_microunits,
            max_tool_calls=total.tool_calls,
        ),
        allowed_capability_ids=capability_ids,
        source_policy_id=settings.source_policy_id,
    )
