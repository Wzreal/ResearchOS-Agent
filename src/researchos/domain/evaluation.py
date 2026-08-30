"""Phase 7 read-only evaluation contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.contracts import (
    BudgetLimits,
    ContractModel,
    OperatingMode,
    SafeId,
    Sha256,
    _require_aware,
    model_sha256,
)
from researchos.domain.identity import (
    normalize_content,
    sha256_text,
    stable_hash,
    stable_id,
)
from researchos.domain.synthesis import VerificationDisposition

EVALUATION_SCHEMA_VERSION = 1
EVALUATION_CANONICALIZATION_VERSION = "canonical-json-v1"
REFERENCE_NORMALIZATION_VERSION = "text-nfc-lines-v1"
METRIC_ROUNDING_VERSION = "decimal-half-even-12-v1"
_METRIC_DECIMAL_QUANTUM = Decimal("0.000000000001")

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
Version = Annotated[str, StringConstraints(min_length=1, max_length=80)]


def normalize_metric_decimal_v1(value: Decimal | int | float) -> float:
    """Return the canonical finite 12-place, half-even metric representation."""

    if type(value) not in {Decimal, int, float}:
        raise TypeError("metric decimal input must be Decimal, int, or float")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        if not decimal_value.is_finite():
            raise ValueError("metric decimal must be finite")
        normalized = decimal_value.quantize(
            _METRIC_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN
        )
    except InvalidOperation as exc:
        raise ValueError("metric decimal cannot be normalized") from exc
    result = float(normalized)
    return 0.0 if result == 0.0 else result


class ReferenceLevel(StrEnum):
    STRUCTURAL_ONLY = "structural_only"
    PARTIAL_GOLD = "partial_gold"
    FULL_REFERENCE = "full_reference"


class Difficulty(StrEnum):
    UNSPECIFIED = "unspecified"
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class SourcePolicyRequirementKind(StrEnum):
    UNSPECIFIED = "unspecified"
    REQUIRE_NONE = "require_none"
    REQUIRE_EXACT = "require_exact"


class SourcePolicyRequirement(ContractModel):
    kind: SourcePolicyRequirementKind = SourcePolicyRequirementKind.UNSPECIFIED
    policy_id: SafeId | None = None

    @model_validator(mode="after")
    def exact_has_one_value(self) -> SourcePolicyRequirement:
        if (self.kind is SourcePolicyRequirementKind.REQUIRE_EXACT) != (
            self.policy_id is not None
        ):
            raise ValueError("only REQUIRE_EXACT carries a source policy ID")
        return self


class BudgetConditionKind(StrEnum):
    EXACT = "exact"
    MINIMUM = "minimum"


class BudgetCondition(ContractModel):
    kind: BudgetConditionKind
    limits: BudgetLimits


class RequiredExecutionConditions(ContractModel):
    source_policy: SourcePolicyRequirement = Field(
        default_factory=SourcePolicyRequirement
    )
    operating_mode: OperatingMode | None = None
    output_format: (
        Annotated[str, StringConstraints(min_length=1, max_length=80)] | None
    ) = None
    budget: BudgetCondition | None = None
    required_allowed_capability_ids: tuple[SafeId, ...] | None = None

    @field_validator("required_allowed_capability_ids")
    @classmethod
    def required_capabilities_are_unique(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("required allowed capability IDs must be unique")
        return value


class ExpectedPlanningTask(ContractModel):
    reference_task_id: SafeId
    objective: ShortText
    normalized_objective_hash: Sha256
    required_capability_ids: tuple[SafeId, ...] = ()
    dependency_reference_task_ids: tuple[SafeId, ...] = ()
    required: bool = True

    @model_validator(mode="after")
    def objective_hash_matches(self) -> ExpectedPlanningTask:
        if self.normalized_objective_hash != sha256_text(
            normalize_content(self.objective)
        ):
            raise ValueError("reference task objective hash differs")
        if len(self.required_capability_ids) != len(
            set(self.required_capability_ids)
        ) or len(self.dependency_reference_task_ids) != len(
            set(self.dependency_reference_task_ids)
        ):
            raise ValueError("reference task IDs must be unique")
        return self


class ExpectedClaim(ContractModel):
    reference_claim_id: SafeId
    statement: ShortText
    normalized_statement_hash: Sha256
    required: bool = True

    @model_validator(mode="after")
    def statement_hash_matches(self) -> ExpectedClaim:
        if self.normalized_statement_hash != sha256_text(
            normalize_content(self.statement)
        ):
            raise ValueError("reference claim statement hash differs")
        return self


class ExpectedEvidenceConstraint(ContractModel):
    constraint_id: SafeId
    source_type: SafeId | None = None
    canonical_locator_hash: Sha256 | None = None
    content_hashes: tuple[Sha256, ...] = ()
    relation: ClaimEvidenceRelation | None = None
    minimum_count: int = Field(default=1, ge=1)


class ExpectedCitationConstraint(ContractModel):
    reference_claim_id: SafeId
    evidence_constraint_id: SafeId
    relation: ClaimEvidenceRelation = ClaimEvidenceRelation.SUPPORTS


class ReferenceAnnotations(ContractModel):
    expected_capability_ids: tuple[SafeId, ...] = ()
    planning_tasks: tuple[ExpectedPlanningTask, ...] = ()
    claims: tuple[ExpectedClaim, ...] = ()
    evidence: tuple[ExpectedEvidenceConstraint, ...] = ()
    citations: tuple[ExpectedCitationConstraint, ...] = ()
    expected_dispositions: tuple[VerificationDisposition, ...] = ()

    @model_validator(mode="after")
    def identities_are_unique_and_referenced(self) -> ReferenceAnnotations:
        collections = (
            (self.expected_capability_ids, None),
            (self.planning_tasks, "reference_task_id"),
            (self.claims, "reference_claim_id"),
            (self.evidence, "constraint_id"),
        )
        for values, attribute in collections:
            identities = (
                list(values)
                if attribute is None
                else [getattr(item, attribute) for item in values]
            )
            if len(identities) != len(set(identities)):
                raise ValueError("reference annotation identities must be unique")
        task_ids = {item.reference_task_id for item in self.planning_tasks}
        if any(
            not set(item.dependency_reference_task_ids).issubset(task_ids)
            for item in self.planning_tasks
        ):
            raise ValueError("reference task dependency is missing")
        claim_ids = {item.reference_claim_id for item in self.claims}
        evidence_ids = {item.constraint_id for item in self.evidence}
        if any(
            item.reference_claim_id not in claim_ids
            or item.evidence_constraint_id not in evidence_ids
            for item in self.citations
        ):
            raise ValueError("citation reference annotation is dangling")
        return self

    @property
    def has_reference(self) -> bool:
        return any(
            (
                self.expected_capability_ids,
                self.planning_tasks,
                self.claims,
                self.evidence,
                self.citations,
                self.expected_dispositions,
            )
        )


class DatasetProvenance(ContractModel):
    license_id: SafeId
    source_uri_hash: Sha256
    curator_id: SafeId
    provenance_version: Version


class EvaluationCase(ContractModel):
    case_id: SafeId
    reference_level: ReferenceLevel
    query: Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
    required_execution_conditions: RequiredExecutionConditions = Field(
        default_factory=RequiredExecutionConditions
    )
    reference_annotations: ReferenceAnnotations = Field(
        default_factory=ReferenceAnnotations
    )
    tags: tuple[SafeId, ...] = ()
    difficulty: Difficulty = Difficulty.UNSPECIFIED
    weight: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query")
    @classmethod
    def query_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evaluation query must not be blank")
        return value

    @model_validator(mode="after")
    def reference_level_matches_annotations(self) -> EvaluationCase:
        present = self.reference_annotations.has_reference
        if self.reference_level is ReferenceLevel.STRUCTURAL_ONLY and present:
            raise ValueError("structural-only case cannot contain reference gold")
        if self.reference_level is not ReferenceLevel.STRUCTURAL_ONLY and not present:
            raise ValueError("reference case requires at least one annotation")
        if self.reference_level is ReferenceLevel.FULL_REFERENCE and not all(
            (
                self.reference_annotations.planning_tasks,
                self.reference_annotations.claims,
                self.reference_annotations.evidence,
                self.reference_annotations.citations,
                self.reference_annotations.expected_dispositions,
            )
        ):
            raise ValueError("full-reference case requires every reference block")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("case tags must be unique")
        if len(stable_hash(self.metadata)) != 64:
            raise ValueError("case metadata must be finite canonical JSON")
        return self


class EvaluationDataset(ContractModel):
    schema_version: Literal[EVALUATION_SCHEMA_VERSION] = EVALUATION_SCHEMA_VERSION
    dataset_id: SafeId
    dataset_version: Version
    provenance: DatasetProvenance
    cases: tuple[EvaluationCase, ...]
    dataset_content_hash: Sha256

    @model_validator(mode="after")
    def dataset_is_canonical(self) -> EvaluationDataset:
        case_ids = [item.case_id for item in self.cases]
        if not case_ids or len(case_ids) != len(set(case_ids)):
            raise ValueError("dataset case IDs must be non-empty and unique")
        if case_ids != sorted(case_ids):
            raise ValueError("dataset cases must be sorted by case ID")
        expected = stable_hash(
            self.model_dump(mode="json", exclude={"dataset_content_hash"})
        )
        if self.dataset_content_hash != expected:
            raise ValueError("dataset content hash differs")
        return self


class ArtifactKind(StrEnum):
    RUN_STATE = "run_state"
    CHECKPOINT = "checkpoint"
    TRACE = "trace"
    EVIDENCE = "evidence"
    CLAIMS = "claims"
    VERIFICATION = "verification"
    REPORT = "report"


class ArtifactAvailability(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    CORRUPT = "corrupt"


class ArtifactSnapshotRef(ContractModel):
    artifact_kind: ArtifactKind
    relative_path: Annotated[str, StringConstraints(min_length=1, max_length=1_024)]
    availability: ArtifactAvailability
    sha256: Sha256 | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    schema_version: int | None = Field(default=None, ge=1)
    corruption_reason_code: SafeId | None = None

    @model_validator(mode="after")
    def presence_has_bytes(self) -> ArtifactSnapshotRef:
        has_bytes = self.sha256 is not None and self.size_bytes is not None
        if self.availability is ArtifactAvailability.ABSENT:
            if (
                has_bytes
                or self.schema_version is not None
                or self.corruption_reason_code
            ):
                raise ValueError("absent artifact cannot carry byte metadata")
        elif not has_bytes:
            raise ValueError("present or corrupt artifact requires hash and size")
        if (self.availability is ArtifactAvailability.CORRUPT) != (
            self.corruption_reason_code is not None
        ):
            raise ValueError("only corrupt artifact carries a corruption reason")
        return self


class RunArtifactCompleteness(StrEnum):
    TERMINAL = "terminal"
    NONTERMINAL_SNAPSHOT = "nonterminal_snapshot"
    CORRUPT = "corrupt"


class RunArtifactManifest(ContractModel):
    run_id: SafeId
    run_revision: int | None = Field(default=None, ge=0)
    run_status: SafeId | None = None
    completeness: RunArtifactCompleteness
    artifacts: tuple[ArtifactSnapshotRef, ...]
    manifest_hash: Sha256

    @model_validator(mode="after")
    def manifest_is_canonical(self) -> RunArtifactManifest:
        kinds = [item.artifact_kind for item in self.artifacts]
        if len(kinds) != len(set(kinds)) or kinds != sorted(kinds, key=str):
            raise ValueError("artifact refs must be unique and sorted")
        expected = stable_hash(self.model_dump(mode="json", exclude={"manifest_hash"}))
        if self.manifest_hash != expected:
            raise ValueError("run artifact manifest hash differs")
        state = next(
            item
            for item in self.artifacts
            if item.artifact_kind is ArtifactKind.RUN_STATE
        )
        valid_state = state.availability is ArtifactAvailability.PRESENT
        if valid_state != (
            self.run_revision is not None and self.run_status is not None
        ):
            raise ValueError("run identity metadata requires a valid state artifact")
        if self.completeness is RunArtifactCompleteness.CORRUPT and not any(
            item.availability is ArtifactAvailability.CORRUPT
            for item in self.artifacts
        ):
            raise ValueError("corrupt completeness requires a corrupt artifact")
        return self


class SUTPinStatus(StrEnum):
    ARTIFACT_VERIFIED = "artifact_verified"
    DECLARED = "declared"
    UNOBSERVABLE = "unobservable"
    ABSENT_BY_DESIGN = "absent_by_design"


class SUTPinSupportKind(StrEnum):
    ARTIFACT = "artifact"
    DECLARATION = "declaration"
    ABSENCE_DECLARATION = "absence_declaration"


class SUTPinSupportRef(ContractModel):
    kind: SUTPinSupportKind
    input_manifest_hash: Sha256
    artifact_kind: ArtifactKind | None = None
    artifact_sha256: Sha256 | None = None


class SUTPinObservation(ContractModel):
    pin_id: SafeId
    pin_version: Version
    value_hash: Sha256 | None = None
    status: SUTPinStatus
    supporting_ref: SUTPinSupportRef
    reason_code: SafeId | None = None

    @model_validator(mode="after")
    def value_matches_status(self) -> SUTPinObservation:
        has_value = self.value_hash is not None
        if self.status in {SUTPinStatus.ARTIFACT_VERIFIED, SUTPinStatus.DECLARED}:
            if not has_value:
                raise ValueError("verified or declared SUT pin requires a value")
        elif has_value:
            raise ValueError("unobservable or absent SUT pin cannot have a value")
        if self.status is SUTPinStatus.ARTIFACT_VERIFIED and (
            self.supporting_ref.kind is not SUTPinSupportKind.ARTIFACT
            or self.supporting_ref.artifact_kind is None
            or self.supporting_ref.artifact_sha256 is None
        ):
            raise ValueError("artifact-verified SUT pin requires artifact support")
        if self.status is SUTPinStatus.DECLARED and (
            self.supporting_ref.kind is not SUTPinSupportKind.DECLARATION
            or self.supporting_ref.artifact_sha256 is not None
        ):
            raise ValueError("declared SUT pin requires declaration support")
        if self.status is SUTPinStatus.UNOBSERVABLE and self.reason_code is None:
            raise ValueError("unobservable SUT pin requires a reason")
        if self.status is SUTPinStatus.ABSENT_BY_DESIGN and (
            self.reason_code is None
            or self.supporting_ref.kind
            is not SUTPinSupportKind.ABSENCE_DECLARATION
            or self.supporting_ref.artifact_kind is None
            or self.supporting_ref.artifact_sha256 is not None
        ):
            raise ValueError("design absence requires explicit absence support")
        return self


class ProvenanceGrade(StrEnum):
    ARTIFACT_VERIFIED = "artifact_verified"
    PARTIALLY_VERIFIED = "partially_verified"
    DECLARED = "declared"


class SystemUnderTest(ContractModel):
    commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    system_version: Version
    observations: tuple[SUTPinObservation, ...]
    provenance_grade: ProvenanceGrade
    system_hash: Sha256

    @model_validator(mode="after")
    def system_is_canonical(self) -> SystemUnderTest:
        keys = [(item.pin_id, item.pin_version) for item in self.observations]
        if len(keys) != len(set(keys)) or keys != sorted(keys):
            raise ValueError("SUT pins must be unique and sorted")
        expected = stable_hash(self.model_dump(mode="json", exclude={"system_hash"}))
        if self.system_hash != expected:
            raise ValueError("SUT hash differs")
        return self


class EvaluationPolicy(ContractModel):
    policy_id: SafeId
    policy_version: Version
    max_cases: int = Field(default=100, ge=1, le=10_000)
    max_metrics_per_case: int = Field(default=256, ge=1, le=10_000)
    max_dataset_bytes: int = Field(default=10_000_000, ge=1)
    max_input_bytes_per_case: int = Field(default=50_000_000, ge=1)
    max_case_artifact_bytes: int = Field(default=2_000_000, ge=1)
    max_evaluation_artifact_bytes: int = Field(default=20_000_000, ge=1)
    max_model_context_bytes: int = Field(default=1_000_000, ge=1)
    max_model_response_bytes: int = Field(default=256_000, ge=1)
    max_model_calls: int = Field(default=0, ge=0)
    max_duration_milliseconds: int = Field(default=300_000, ge=1)
    allow_nonterminal_runs: bool = False
    enable_model_evaluators: bool = False
    enabled_evaluator_ids: tuple[SafeId, ...]

    @model_validator(mode="after")
    def model_and_evaluator_bounds_match(self) -> EvaluationPolicy:
        if not self.enabled_evaluator_ids or len(self.enabled_evaluator_ids) != len(
            set(self.enabled_evaluator_ids)
        ):
            raise ValueError("enabled evaluator IDs must be non-empty and unique")
        if self.enabled_evaluator_ids != tuple(sorted(self.enabled_evaluator_ids)):
            raise ValueError("enabled evaluator IDs must be sorted")
        if self.enable_model_evaluators != (self.max_model_calls > 0):
            raise ValueError("model enablement and global call bound must agree")
        if self.enable_model_evaluators != (
            "model_evaluator" in self.enabled_evaluator_ids
        ):
            raise ValueError("model evaluator enablement must be explicit")
        return self


class CaseRunBinding(ContractModel):
    case_id: SafeId
    run_id: SafeId
    expected_manifest_hash: Sha256 | None = None
    absent_by_design_pin_ids: tuple[SafeId, ...] = ()

    @field_validator("absent_by_design_pin_ids")
    @classmethod
    def absence_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("absent-by-design pin IDs must be unique")
        return value


class EvaluationRequest(ContractModel):
    dataset_id: SafeId
    dataset_version: Version
    dataset_hash: Sha256
    bindings: tuple[CaseRunBinding, ...]
    commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    system_version: Version
    declared_sut_observations: tuple[SUTPinObservation, ...] = ()
    policy: EvaluationPolicy
    deadline: datetime

    _aware_deadline = field_validator("deadline")(_require_aware)

    @model_validator(mode="after")
    def bindings_are_unique(self) -> EvaluationRequest:
        case_ids = [item.case_id for item in self.bindings]
        run_ids = [item.run_id for item in self.bindings]
        if (
            not case_ids
            or len(case_ids) != len(set(case_ids))
            or len(run_ids) != len(set(run_ids))
        ):
            raise ValueError("evaluation bindings require unique case and run IDs")
        if case_ids != sorted(case_ids):
            raise ValueError("evaluation bindings must be sorted by case ID")
        if any(
            item.status is not SUTPinStatus.DECLARED
            for item in self.declared_sut_observations
        ):
            raise ValueError("request SUT observations must be declarations")
        return self


class MetricLayer(StrEnum):
    PLANNING = "planning"
    EXECUTION = "execution"
    EVIDENCE = "evidence"
    VERIFICATION = "verification"


class MetricValueKind(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    FLOAT = "float"
    RATIO = "ratio"
    ENUM = "enum"
    NON_COMPUTED = "non_computed"


class MetricStatus(StrEnum):
    COMPUTED = "computed"
    NOT_APPLICABLE = "not_applicable"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    TARGET = "target"
    INFORMATIONAL = "informational"


class MetricCertainty(StrEnum):
    EXACT = "exact"
    UPPER_BOUND = "upper_bound"
    MODEL_ASSESSED = "model_assessed"


class MetricEvidenceProvenance(StrEnum):
    AUTHORITATIVE = "authoritative"
    RECOMPUTED = "recomputed"
    SUPPLEMENTAL_TRACE = "supplemental_trace"


class MetricEvidenceRef(ContractModel):
    artifact_kind: ArtifactKind
    input_manifest_hash: Sha256
    artifact_sha256: Sha256 | None = None
    entity_id: SafeId | None = None
    revision: int | None = Field(default=None, ge=0)
    field_selector: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    provenance: MetricEvidenceProvenance
    trace_event_id: SafeId | None = None
    algorithm_version: Version

    @model_validator(mode="after")
    def trace_provenance_is_explicit(self) -> MetricEvidenceRef:
        if (self.provenance is MetricEvidenceProvenance.SUPPLEMENTAL_TRACE) != (
            self.trace_event_id is not None
        ):
            raise ValueError("supplemental trace evidence requires a trace event ID")
        return self


class AggregationKind(StrEnum):
    NONE = "none"
    NUMERIC = "numeric"
    RATIO_MACRO_MICRO = "ratio_macro_micro"
    BOOLEAN_RATE = "boolean_rate"
    ENUM_DISTRIBUTION = "enum_distribution"


class MetricDefinitionSnapshot(ContractModel):
    metric_id: SafeId
    metric_version: Version
    layer: MetricLayer
    value_kind: MetricValueKind
    unit: SafeId
    direction: MetricDirection
    evaluator_id: SafeId
    evaluator_version: Version
    normalization_versions: tuple[Version, ...] = ()
    applicability_version: Version
    aggregation_kind: AggregationKind
    rounding_version: Version = METRIC_ROUNDING_VERSION
    definition_hash: Sha256

    @model_validator(mode="after")
    def definition_hash_matches(self) -> MetricDefinitionSnapshot:
        expected = stable_hash(
            self.model_dump(mode="json", exclude={"definition_hash"})
        )
        if self.definition_hash != expected:
            raise ValueError("metric definition hash differs")
        return self


class _ComputedMetric(ContractModel):
    metric_id: SafeId
    definition_hash: Sha256
    status: Literal[MetricStatus.COMPUTED] = MetricStatus.COMPUTED
    certainty: MetricCertainty
    evidence_refs: tuple[MetricEvidenceRef, ...]
    reason_code: SafeId | None = None

    @model_validator(mode="after")
    def computed_has_evidence(self) -> _ComputedMetric:
        if not self.evidence_refs:
            raise ValueError("computed metric requires evidence")
        return self


class BooleanMetricValue(_ComputedMetric):
    value_type: Literal[MetricValueKind.BOOLEAN] = MetricValueKind.BOOLEAN
    value: bool

    @field_validator("value", mode="before")
    @classmethod
    def boolean_is_strict(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("boolean metric value must be a bool")
        return value


class IntegerMetricValue(_ComputedMetric):
    value_type: Literal[MetricValueKind.INTEGER] = MetricValueKind.INTEGER
    value: int

    @field_validator("value", mode="before")
    @classmethod
    def integer_is_strict(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("integer metric value must be an int")
        return value


class FloatMetricValue(_ComputedMetric):
    value_type: Literal[MetricValueKind.FLOAT] = MetricValueKind.FLOAT
    value: float = Field(allow_inf_nan=False)

    @field_validator("value", mode="before")
    @classmethod
    def float_is_strict(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("float metric value must be a float")
        return value

    @model_validator(mode="after")
    def float_is_canonical(self) -> FloatMetricValue:
        if self.value != normalize_metric_decimal_v1(self.value):
            raise ValueError("float metric value is not canonically normalized")
        return self


class RatioMetricValue(_ComputedMetric):
    value_type: Literal[MetricValueKind.RATIO] = MetricValueKind.RATIO
    value: float = Field(ge=0, allow_inf_nan=False)
    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)

    @field_validator("value", mode="before")
    @classmethod
    def ratio_value_is_strict_float(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("ratio value must be a float")
        return value

    @field_validator("numerator", "denominator", mode="before")
    @classmethod
    def counts_are_strict_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("ratio counts must be integers")
        return value

    @model_validator(mode="after")
    def ratio_matches_counts(self) -> RatioMetricValue:
        expected = normalize_metric_decimal_v1(
            Decimal(self.numerator) / Decimal(self.denominator)
        )
        if expected != self.value:
            raise ValueError("ratio value differs from numerator/denominator")
        return self


class EnumMetricValue(_ComputedMetric):
    value_type: Literal[MetricValueKind.ENUM] = MetricValueKind.ENUM
    value: SafeId


class NonComputedMetricValue(ContractModel):
    value_type: Literal[MetricValueKind.NON_COMPUTED] = MetricValueKind.NON_COMPUTED
    metric_id: SafeId
    definition_hash: Sha256
    status: Literal[
        MetricStatus.NOT_APPLICABLE,
        MetricStatus.SKIPPED,
        MetricStatus.UNAVAILABLE,
        MetricStatus.ERROR,
    ]
    reason_code: SafeId
    evidence_refs: tuple[MetricEvidenceRef, ...] = ()


MetricValue = Annotated[
    BooleanMetricValue
    | IntegerMetricValue
    | FloatMetricValue
    | RatioMetricValue
    | EnumMetricValue
    | NonComputedMetricValue,
    Field(discriminator="value_type"),
]


class EvaluationJudgeRequest(ContractModel):
    fixture_key: Sha256
    eval_run_id: SafeId
    case_id: SafeId
    evaluator_id: SafeId
    evaluator_version: Version
    context_hash: Sha256
    context: dict[str, Any]
    expected_metric_ids: tuple[SafeId, ...]


class EvaluationJudgeResponse(ContractModel):
    fixture_key: Sha256
    evaluator_id: SafeId
    evaluator_version: Version
    model_id: SafeId
    model_bundle_hash: Sha256
    raw_response_bytes: int = Field(ge=0)
    metrics: tuple[MetricValue, ...]


class CompatibilityIssue(ContractModel):
    code: SafeId
    field: SafeId


class CaseRunCompatibility(ContractModel):
    case_id: SafeId
    run_id: SafeId
    case_input_hash: Sha256
    run_input_hash: Sha256
    compatible: bool
    issues: tuple[CompatibilityIssue, ...] = ()
    algorithm_version: Literal["case-run-compatibility-v1"] = (
        "case-run-compatibility-v1"
    )

    @model_validator(mode="after")
    def compatibility_matches_issues(self) -> CaseRunCompatibility:
        if self.compatible != (not self.issues):
            raise ValueError("compatibility must mean no issue")
        return self


class EvaluationFailure(ContractModel):
    code: SafeId
    evaluator_id: SafeId | None = None
    artifact_kind: ArtifactKind | None = None


class EvaluationCaseStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class MetricAggregate(ContractModel):
    metric_id: SafeId
    definition_hash: Sha256
    computed_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    not_applicable_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    mean: float | None = Field(default=None, allow_inf_nan=False)
    median: float | None = Field(default=None, allow_inf_nan=False)
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    micro_ratio: float | None = Field(default=None, allow_inf_nan=False)
    enum_distribution: dict[SafeId, int] = Field(default_factory=dict)

    @field_validator("mean", "median", "minimum", "maximum", "micro_ratio")
    @classmethod
    def aggregate_decimals_are_canonical(cls, value: float | None) -> float | None:
        if value is not None and value != normalize_metric_decimal_v1(value):
            raise ValueError("aggregate decimal is not canonically normalized")
        return value


class EvaluationCaseResult(ContractModel):
    case_result_id: SafeId
    eval_run_id: SafeId
    case_id: SafeId
    run_id: SafeId
    input_manifest: RunArtifactManifest
    compatibility: CaseRunCompatibility | None = None
    status: EvaluationCaseStatus
    metrics: tuple[MetricValue, ...]
    failures: tuple[EvaluationFailure, ...] = ()
    sut_observations: tuple[SUTPinObservation, ...]
    started_at: datetime
    completed_at: datetime
    case_semantic_hash: Sha256
    artifact_content_hash: Sha256

    _aware_started = field_validator("started_at")(_require_aware)
    _aware_completed = field_validator("completed_at")(_require_aware)

    @model_validator(mode="after")
    def case_result_is_internally_consistent(self) -> EvaluationCaseResult:
        metric_ids = [item.metric_id for item in self.metrics]
        if len(metric_ids) != len(set(metric_ids)) or metric_ids != sorted(metric_ids):
            raise ValueError("case metrics must be unique and sorted")
        if self.input_manifest.run_id != self.run_id:
            raise ValueError("case result binding identity differs")
        if self.status is not EvaluationCaseStatus.FAILED and (
            self.compatibility is None
            or self.compatibility.case_id != self.case_id
            or self.compatibility.run_id != self.run_id
            or not self.compatibility.compatible
        ):
            raise ValueError("successful case requires compatible binding")
        if self.case_result_id != stable_id(
            "evalcase",
            [
                self.eval_run_id,
                self.case_id,
                self.run_id,
                self.input_manifest.manifest_hash,
            ],
        ):
            raise ValueError("case result identity differs")
        if self.completed_at < self.started_at:
            raise ValueError("case completion precedes start")
        has_failure = bool(self.failures) or any(
            item.status is MetricStatus.ERROR for item in self.metrics
        )
        if self.status is EvaluationCaseStatus.COMPLETED and has_failure:
            raise ValueError("case status differs from metric failures")
        if self.status is EvaluationCaseStatus.PARTIAL and not has_failure:
            raise ValueError("case status differs from metric failures")
        if self.status is EvaluationCaseStatus.FAILED and not self.failures:
            raise ValueError("failed case requires an explicit failure")
        return self


class EvaluationRunStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class EvaluationRun(ContractModel):
    schema_version: Literal[EVALUATION_SCHEMA_VERSION] = EVALUATION_SCHEMA_VERSION
    eval_run_id: SafeId
    dataset_id: SafeId
    dataset_version: Version
    dataset_hash: Sha256
    case_ids: tuple[SafeId, ...]
    system: SystemUnderTest
    evaluation_policy: EvaluationPolicy
    evaluation_policy_hash: Sha256
    metric_definitions: tuple[MetricDefinitionSnapshot, ...]
    evaluator_bundle_hash: Sha256
    evaluation_model_bundle_hash: Sha256 | None = None
    provenance_grade: ProvenanceGrade
    status: EvaluationRunStatus
    cases: tuple[EvaluationCaseResult, ...]
    aggregates: tuple[MetricAggregate, ...]
    started_at: datetime
    completed_at: datetime
    evaluation_semantic_hash: Sha256
    artifact_content_hash: Sha256

    _aware_started = field_validator("started_at")(_require_aware)
    _aware_completed = field_validator("completed_at")(_require_aware)

    @model_validator(mode="after")
    def evaluation_run_is_internally_consistent(self) -> EvaluationRun:
        if self.completed_at < self.started_at:
            raise ValueError("evaluation completion precedes start")
        if self.evaluation_policy_hash != model_sha256(self.evaluation_policy):
            raise ValueError("evaluation policy hash differs")
        definition_ids = [item.metric_id for item in self.metric_definitions]
        if (
            len(definition_ids) != len(set(definition_ids))
            or definition_ids != sorted(definition_ids)
        ):
            raise ValueError("metric definitions must be unique and sorted")
        expected_bundle = stable_hash(
            {
                "definitions": [
                    [item.evaluator_id, item.evaluator_version, item.definition_hash]
                    for item in self.metric_definitions
                ],
                "model_bundle_hash": self.evaluation_model_bundle_hash,
            }
        )
        if self.evaluator_bundle_hash != expected_bundle:
            raise ValueError("evaluator bundle hash differs")
        case_ids = [item.case_id for item in self.cases]
        if (
            self.case_ids != tuple(case_ids)
            or len(case_ids) != len(set(case_ids))
            or case_ids != sorted(case_ids)
            or any(item.eval_run_id != self.eval_run_id for item in self.cases)
        ):
            raise ValueError("evaluation case identity/order differs")
        definitions = {
            item.metric_id: item.definition_hash for item in self.metric_definitions
        }
        if any(
            definitions.get(metric.metric_id) != metric.definition_hash
            for case in self.cases
            for metric in case.metrics
        ):
            raise ValueError("case metric definition differs")
        aggregates = {
            item.metric_id: item.definition_hash for item in self.aggregates
        }
        if aggregates != definitions or len(aggregates) != len(self.aggregates):
            raise ValueError("aggregate metric definitions differ")
        if self.provenance_grade is not self.system.provenance_grade:
            raise ValueError("evaluation provenance differs from SUT")
        expected_status = (
            EvaluationRunStatus.FAILED
            if all(item.status is EvaluationCaseStatus.FAILED for item in self.cases)
            else EvaluationRunStatus.PARTIAL
            if any(
                item.status is not EvaluationCaseStatus.COMPLETED
                for item in self.cases
            )
            else EvaluationRunStatus.COMPLETED
        )
        if self.status is not expected_status:
            raise ValueError("evaluation status differs from cases")
        if self.eval_run_id != stable_id(
            "eval",
            [
                self.dataset_hash,
                self.system.system_hash,
                self.evaluation_policy_hash,
                self.evaluator_bundle_hash,
                [
                    [
                        item.case_id,
                        item.run_id,
                        item.input_manifest.manifest_hash,
                    ]
                    for item in self.cases
                ],
            ],
        ):
            raise ValueError("evaluation run identity differs")
        return self


class ThresholdKind(StrEnum):
    NO_DECREASE = "no_decrease"
    ABSOLUTE_DELTA = "absolute_delta"
    RELATIVE_DELTA = "relative_delta"


class MissingMetricBehavior(StrEnum):
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class AggregateStatistic(StrEnum):
    MEAN = "mean"
    MICRO_RATIO = "micro_ratio"


class RegressionRule(ContractModel):
    metric_id: SafeId
    definition_hash: Sha256
    threshold_kind: ThresholdKind
    allowed_degradation: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    missing_behavior: MissingMetricBehavior = MissingMetricBehavior.FAIL
    aggregate_statistic: AggregateStatistic = AggregateStatistic.MEAN

    @model_validator(mode="after")
    def threshold_semantics_are_explicit(self) -> RegressionRule:
        if (
            self.threshold_kind is ThresholdKind.NO_DECREASE
            and self.allowed_degradation != 0
        ):
            raise ValueError("NO_DECREASE requires zero allowed degradation")
        if self.allowed_degradation != normalize_metric_decimal_v1(
            self.allowed_degradation
        ):
            raise ValueError("regression threshold is not canonically normalized")
        return self


class RegressionPolicy(ContractModel):
    policy_id: SafeId
    policy_version: Version
    rules: tuple[RegressionRule, ...]
    policy_hash: Sha256

    @model_validator(mode="after")
    def policy_is_canonical(self) -> RegressionPolicy:
        ids = [item.metric_id for item in self.rules]
        if not ids or len(ids) != len(set(ids)) or ids != sorted(ids):
            raise ValueError("regression rules must be non-empty, unique, and sorted")
        if self.policy_hash != stable_hash(
            self.model_dump(mode="json", exclude={"policy_hash"})
        ):
            raise ValueError("regression policy hash differs")
        return self


class DeltaClassification(StrEnum):
    REGRESSION = "regression"
    IMPROVEMENT = "improvement"
    UNCHANGED = "unchanged"
    NOT_COMPARABLE = "not_comparable"


class ComparisonDisposition(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class MetricDelta(ContractModel):
    metric_id: SafeId
    definition_hash: Sha256
    baseline: float | None = Field(default=None, allow_inf_nan=False)
    candidate: float | None = Field(default=None, allow_inf_nan=False)
    absolute_delta: float | None = Field(default=None, allow_inf_nan=False)
    relative_delta: float | None = Field(default=None, allow_inf_nan=False)
    classification: DeltaClassification
    reason_code: SafeId | None = None

    @field_validator("baseline", "candidate", "absolute_delta", "relative_delta")
    @classmethod
    def delta_decimals_are_canonical(cls, value: float | None) -> float | None:
        if value is not None and value != normalize_metric_decimal_v1(value):
            raise ValueError("comparison decimal is not canonically normalized")
        return value


class EvaluationComparison(ContractModel):
    comparison_id: SafeId
    baseline_eval_run_id: SafeId
    baseline_semantic_hash: Sha256
    candidate_eval_run_id: SafeId
    candidate_semantic_hash: Sha256
    regression_policy_hash: Sha256
    provenance_grade: ProvenanceGrade
    disposition: ComparisonDisposition
    deltas: tuple[MetricDelta, ...]
    artifact_content_hash: Sha256

    @model_validator(mode="after")
    def comparison_identity_matches(self) -> EvaluationComparison:
        if self.comparison_id != stable_id(
            "cmp",
            [
                self.baseline_eval_run_id,
                self.baseline_semantic_hash,
                self.candidate_eval_run_id,
                self.candidate_semantic_hash,
                self.regression_policy_hash,
            ],
        ):
            raise ValueError("comparison identity differs")
        expected_hash = stable_hash(
            self.model_dump(mode="json", exclude={"artifact_content_hash"})
        )
        if self.artifact_content_hash != expected_hash:
            raise ValueError("comparison artifact hash differs")
        return self


class AblationCausality(StrEnum):
    VERIFIED_CAUSAL_COMPARISON = "verified_causal_comparison"
    PARTIALLY_VERIFIED_COMPARISON = "partially_verified_comparison"
    DECLARED_ASSOCIATION = "declared_association"


class AblationArm(ContractModel):
    arm_id: SafeId
    eval_run_id: SafeId
    declared_config_hash: Sha256
    changed_pin_ids: tuple[SafeId, ...]
    absent_by_design_pin_ids: tuple[SafeId, ...] = ()

    @model_validator(mode="after")
    def pin_sets_are_canonical(self) -> AblationArm:
        for values in (self.changed_pin_ids, self.absent_by_design_pin_ids):
            if len(values) != len(set(values)) or values != tuple(sorted(values)):
                raise ValueError("ablation pin IDs must be unique and sorted")
        if set(self.changed_pin_ids) & set(self.absent_by_design_pin_ids):
            raise ValueError("changed and absent pin IDs must be disjoint")
        return self


class AblationSpec(ContractModel):
    ablation_id: SafeId
    ablation_spec_hash: Sha256
    dataset_hash: Sha256
    evaluation_policy_hash: Sha256
    baseline: AblationArm
    candidates: tuple[AblationArm, ...]
    one_dimension_only: bool = True

    @model_validator(mode="after")
    def arms_are_canonical(self) -> AblationSpec:
        arm_ids = [item.arm_id for item in self.candidates]
        run_ids = [item.eval_run_id for item in self.candidates]
        if (
            not arm_ids
            or len(arm_ids) != len(set(arm_ids))
            or len(run_ids) != len(set(run_ids))
            or arm_ids != sorted(arm_ids)
            or self.baseline.eval_run_id in set(run_ids)
        ):
            raise ValueError("ablation candidate arms must be unique and sorted")
        expected_hash = stable_hash(
            self.model_dump(
                mode="json", exclude={"ablation_id", "ablation_spec_hash"}
            )
        )
        if self.ablation_spec_hash != expected_hash:
            raise ValueError("ablation spec hash differs")
        if self.ablation_id != stable_id("ablation", [expected_hash]):
            raise ValueError("ablation identity differs")
        return self


class AblationResult(ContractModel):
    ablation_id: SafeId
    ablation_spec_hash: Sha256
    ablation_spec: AblationSpec
    comparisons: tuple[EvaluationComparison, ...]
    causality: AblationCausality
    provenance_grade: ProvenanceGrade
    artifact_content_hash: Sha256

    @model_validator(mode="after")
    def ablation_hash_matches(self) -> AblationResult:
        if (
            self.ablation_id != self.ablation_spec.ablation_id
            or self.ablation_spec_hash != self.ablation_spec.ablation_spec_hash
        ):
            raise ValueError("ablation result does not bind its complete spec")
        candidate_ids = [item.candidate_eval_run_id for item in self.comparisons]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("ablation comparisons contain duplicate candidates")
        if set(candidate_ids) != {
            item.eval_run_id for item in self.ablation_spec.candidates
        }:
            raise ValueError(
                "ablation comparisons do not cover the declared candidates"
            )
        expected = stable_hash(
            self.model_dump(mode="json", exclude={"artifact_content_hash"})
        )
        if self.artifact_content_hash != expected:
            raise ValueError("ablation artifact hash differs")
        return self
