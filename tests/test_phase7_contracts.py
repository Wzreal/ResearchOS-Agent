from __future__ import annotations

import pytest
from pydantic import ValidationError

from researchos.application.evaluation_compatibility import (
    validate_sut_homogeneity,
)
from researchos.application.evaluation_evaluators import DETERMINISTIC_DEFINITIONS
from researchos.domain.evaluation import (
    ArtifactKind,
    BooleanMetricValue,
    EvaluationPolicy,
    IntegerMetricValue,
    MetricCertainty,
    MetricEvidenceProvenance,
    MetricEvidenceRef,
    MetricStatus,
    NonComputedMetricValue,
    ProvenanceGrade,
    RatioMetricValue,
    SourcePolicyRequirement,
    SourcePolicyRequirementKind,
    SUTPinObservation,
    SUTPinStatus,
    SUTPinSupportKind,
    SUTPinSupportRef,
)
from researchos.domain.identity import stable_hash

HASH = "0" * 64


def evidence_ref() -> MetricEvidenceRef:
    return MetricEvidenceRef(
        artifact_kind=ArtifactKind.CHECKPOINT,
        input_manifest_hash=HASH,
        artifact_sha256=HASH,
        field_selector="checkpoint",
        provenance=MetricEvidenceProvenance.RECOMPUTED,
        algorithm_version="1",
    )


def test_source_policy_requirement_has_three_unambiguous_states():
    assert SourcePolicyRequirement().kind is SourcePolicyRequirementKind.UNSPECIFIED
    assert (
        SourcePolicyRequirement(kind=SourcePolicyRequirementKind.REQUIRE_NONE).policy_id
        is None
    )
    exact = SourcePolicyRequirement(
        kind=SourcePolicyRequirementKind.REQUIRE_EXACT, policy_id="policy_a"
    )
    assert exact.policy_id == "policy_a"
    with pytest.raises(ValidationError):
        SourcePolicyRequirement(kind=SourcePolicyRequirementKind.REQUIRE_EXACT)
    with pytest.raises(ValidationError):
        SourcePolicyRequirement(
            kind=SourcePolicyRequirementKind.REQUIRE_NONE, policy_id="policy_a"
        )


def test_metric_value_types_are_strict_and_unavailable_is_not_zero():
    definition = DETERMINISTIC_DEFINITIONS[0]
    with pytest.raises(ValidationError):
        BooleanMetricValue(
            metric_id=definition.metric_id,
            definition_hash=definition.definition_hash,
            value=1,
            certainty=MetricCertainty.EXACT,
            evidence_refs=(evidence_ref(),),
        )
    unavailable = NonComputedMetricValue(
        metric_id=definition.metric_id,
        definition_hash=definition.definition_hash,
        status=MetricStatus.UNAVAILABLE,
        reason_code="artifact_absent",
    )
    assert "value" not in unavailable.model_dump()
    with pytest.raises(ValidationError):
        IntegerMetricValue(
            metric_id="integer_test",
            definition_hash=HASH,
            value=True,
            certainty=MetricCertainty.EXACT,
            evidence_refs=(evidence_ref(),),
        )
    with pytest.raises(ValidationError):
        RatioMetricValue(
            metric_id="ratio_test",
            definition_hash=HASH,
            value=0.0,
            numerator=0,
            denominator=0,
            certainty=MetricCertainty.EXACT,
            evidence_refs=(evidence_ref(),),
        )


def pin(status: SUTPinStatus, value: str | None) -> SUTPinObservation:
    return SUTPinObservation(
        pin_id="planner_bundle",
        pin_version="1",
        value_hash=None if value is None else stable_hash(value),
        status=status,
        reason_code="not_observed" if value is None else None,
        supporting_ref=SUTPinSupportRef(
            kind=(
                SUTPinSupportKind.ARTIFACT
                if status is SUTPinStatus.ARTIFACT_VERIFIED
                else SUTPinSupportKind.DECLARATION
            ),
            input_manifest_hash=HASH,
            artifact_kind=ArtifactKind.CHECKPOINT,
            artifact_sha256=HASH if status is SUTPinStatus.ARTIFACT_VERIFIED else None,
        ),
    )


def test_sut_value_conflict_fails_and_unobservable_downgrades():
    with pytest.raises(Exception, match="value conflict"):
        validate_sut_homogeneity(
            commit_sha="a" * 40,
            system_version="1",
            observations_by_case=(
                (pin(SUTPinStatus.ARTIFACT_VERIFIED, "a"),),
                (pin(SUTPinStatus.ARTIFACT_VERIFIED, "b"),),
            ),
        )
    result = validate_sut_homogeneity(
        commit_sha="a" * 40,
        system_version="1",
        observations_by_case=(
            (pin(SUTPinStatus.ARTIFACT_VERIFIED, "a"),),
            (pin(SUTPinStatus.UNOBSERVABLE, None),),
        ),
    )
    assert result.provenance_grade is ProvenanceGrade.PARTIALLY_VERIFIED


def test_declared_conflict_fails_but_same_declared_value_only_downgrades():
    with pytest.raises(Exception, match="value conflict"):
        validate_sut_homogeneity(
            commit_sha="a" * 40,
            system_version="1",
            observations_by_case=(
                (pin(SUTPinStatus.DECLARED, "a"),),
                (pin(SUTPinStatus.DECLARED, "b"),),
            ),
        )
    result = validate_sut_homogeneity(
        commit_sha="a" * 40,
        system_version="1",
        observations_by_case=(
            (pin(SUTPinStatus.ARTIFACT_VERIFIED, "a"),),
            (pin(SUTPinStatus.DECLARED, "a"),),
        ),
    )
    assert result.provenance_grade is ProvenanceGrade.PARTIALLY_VERIFIED


def test_trace_metric_evidence_is_explicitly_supplemental():
    with pytest.raises(ValidationError):
        MetricEvidenceRef(
            artifact_kind=ArtifactKind.TRACE,
            input_manifest_hash=HASH,
            artifact_sha256=HASH,
            field_selector="events",
            provenance=MetricEvidenceProvenance.SUPPLEMENTAL_TRACE,
            algorithm_version="1",
        )
    ref = MetricEvidenceRef(
        artifact_kind=ArtifactKind.TRACE,
        input_manifest_hash=HASH,
        artifact_sha256=HASH,
        field_selector="events",
        provenance=MetricEvidenceProvenance.SUPPLEMENTAL_TRACE,
        trace_event_id="evt_a",
        algorithm_version="1",
    )
    assert ref.trace_event_id == "evt_a"


def test_evaluation_policy_enforces_model_bound_consistency():
    EvaluationPolicy(
        policy_id="policy",
        policy_version="1",
        enabled_evaluator_ids=("deterministic_core",),
    )
    with pytest.raises(ValidationError):
        EvaluationPolicy(
            policy_id="policy",
            policy_version="1",
            enable_model_evaluators=True,
            max_model_calls=0,
            enabled_evaluator_ids=("deterministic_core",),
        )
