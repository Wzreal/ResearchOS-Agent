"""Definition-safe baseline comparison and ablation classification."""

from __future__ import annotations

from decimal import Decimal

from researchos.application.errors import (
    EvaluationComparisonError,
    EvaluationProvenanceError,
    IncompatibleEvaluationRuns,
    MetricDefinitionCompatibilityError,
)
from researchos.application.evaluation_identity import (
    ablation_artifact_hash,
    comparison_artifact_hash,
)
from researchos.domain.evaluation import (
    AblationCausality,
    AblationResult,
    AblationSpec,
    ComparisonDisposition,
    DeltaClassification,
    EvaluationComparison,
    EvaluationRun,
    MetricDelta,
    MetricDirection,
    MissingMetricBehavior,
    ProvenanceGrade,
    RegressionPolicy,
    SUTPinStatus,
    ThresholdKind,
    normalize_metric_decimal_v1,
)
from researchos.domain.identity import stable_hash, stable_id

_GRADE_ORDER = {
    ProvenanceGrade.DECLARED: 0,
    ProvenanceGrade.PARTIALLY_VERIFIED: 1,
    ProvenanceGrade.ARTIFACT_VERIFIED: 2,
}


def weaker_grade(left: ProvenanceGrade, right: ProvenanceGrade) -> ProvenanceGrade:
    return left if _GRADE_ORDER[left] <= _GRADE_ORDER[right] else right


class EvaluationComparisonService:
    def compare(
        self,
        baseline: EvaluationRun,
        candidate: EvaluationRun,
        policy: RegressionPolicy,
    ) -> EvaluationComparison:
        if (
            baseline.dataset_hash != candidate.dataset_hash
            or baseline.case_ids != candidate.case_ids
            or baseline.evaluation_policy_hash != candidate.evaluation_policy_hash
        ):
            raise IncompatibleEvaluationRuns(
                "comparison requires the same dataset, cases, and evaluation policy"
            )
        if baseline.evaluator_bundle_hash != candidate.evaluator_bundle_hash:
            raise MetricDefinitionCompatibilityError("evaluator bundle differs")
        base_definitions = {
            item.metric_id: item for item in baseline.metric_definitions
        }
        candidate_definitions = {
            item.metric_id: item for item in candidate.metric_definitions
        }
        for rule in policy.rules:
            left = base_definitions.get(rule.metric_id)
            right = candidate_definitions.get(rule.metric_id)
            if (
                left is None
                or right is None
                or left.definition_hash != rule.definition_hash
                or right.definition_hash != rule.definition_hash
            ):
                raise MetricDefinitionCompatibilityError(
                    f"metric definition differs for {rule.metric_id}"
                )
        base = {item.metric_id: item for item in baseline.aggregates}
        cand = {item.metric_id: item for item in candidate.aggregates}
        deltas: list[MetricDelta] = []
        failed = False
        inconclusive = False
        for rule in policy.rules:
            left = base.get(rule.metric_id)
            right = cand.get(rule.metric_id)
            statistic = rule.aggregate_statistic.value
            left_value = None if left is None else getattr(left, statistic)
            right_value = None if right is None else getattr(right, statistic)
            if left_value is None or right_value is None:
                failed |= rule.missing_behavior is MissingMetricBehavior.FAIL
                inconclusive |= (
                    rule.missing_behavior is MissingMetricBehavior.INCONCLUSIVE
                )
                deltas.append(
                    MetricDelta(
                        metric_id=rule.metric_id,
                        definition_hash=rule.definition_hash,
                        baseline=left_value,
                        candidate=right_value,
                        classification=DeltaClassification.NOT_COMPARABLE,
                        reason_code="metric_not_computed",
                    )
                )
                continue
            absolute = normalize_metric_decimal_v1(
                Decimal(str(right_value)) - Decimal(str(left_value))
            )
            relative = (
                None
                if left_value == 0
                else normalize_metric_decimal_v1(
                    Decimal(str(absolute)) / abs(Decimal(str(left_value)))
                )
            )
            direction = base_definitions[rule.metric_id].direction
            degradation = (
                -absolute if direction is MetricDirection.HIGHER_IS_BETTER else absolute
            )
            if direction in {MetricDirection.INFORMATIONAL, MetricDirection.TARGET}:
                raise EvaluationComparisonError(
                    "regression rule requires a directional metric"
                )
            if rule.threshold_kind in {
                ThresholdKind.NO_DECREASE,
                ThresholdKind.ABSOLUTE_DELTA,
            }:
                regressed = degradation > rule.allowed_degradation
            else:
                if relative is None:
                    inconclusive = True
                    deltas.append(
                        MetricDelta(
                            metric_id=rule.metric_id,
                            definition_hash=rule.definition_hash,
                            baseline=left_value,
                            candidate=right_value,
                            absolute_delta=absolute,
                            classification=DeltaClassification.NOT_COMPARABLE,
                            reason_code="baseline_zero_relative_delta",
                        )
                    )
                    continue
                signed_relative_degradation = (
                    -relative
                    if direction is MetricDirection.HIGHER_IS_BETTER
                    else relative
                )
                regressed = signed_relative_degradation > rule.allowed_degradation
            failed |= regressed
            improvement_measure = (
                -degradation
                if rule.threshold_kind is not ThresholdKind.RELATIVE_DELTA
                else -signed_relative_degradation
            )
            improved = improvement_measure > rule.allowed_degradation
            deltas.append(
                MetricDelta(
                    metric_id=rule.metric_id,
                    definition_hash=rule.definition_hash,
                    baseline=left_value,
                    candidate=right_value,
                    absolute_delta=absolute,
                    relative_delta=relative,
                    classification=(
                        DeltaClassification.REGRESSION
                        if regressed
                        else DeltaClassification.IMPROVEMENT
                        if improved
                        else DeltaClassification.UNCHANGED
                    ),
                )
            )
        disposition = (
            ComparisonDisposition.FAIL
            if failed
            else ComparisonDisposition.INCONCLUSIVE
            if inconclusive
            else ComparisonDisposition.PASS
        )
        grade = weaker_grade(baseline.provenance_grade, candidate.provenance_grade)
        comparison_id = stable_id(
            "cmp",
            [
                baseline.eval_run_id,
                baseline.evaluation_semantic_hash,
                candidate.eval_run_id,
                candidate.evaluation_semantic_hash,
                policy.policy_hash,
            ],
        )
        values = dict(
            comparison_id=comparison_id,
            baseline_eval_run_id=baseline.eval_run_id,
            baseline_semantic_hash=baseline.evaluation_semantic_hash,
            candidate_eval_run_id=candidate.eval_run_id,
            candidate_semantic_hash=candidate.evaluation_semantic_hash,
            regression_policy_hash=policy.policy_hash,
            provenance_grade=grade,
            disposition=disposition,
            deltas=tuple(deltas),
        )
        provisional = EvaluationComparison.model_construct(
            **values, artifact_content_hash="0" * 64
        )
        return EvaluationComparison(
            **values,
            artifact_content_hash=comparison_artifact_hash(provisional),
        )


class AblationService:
    def validate(
        self,
        spec: AblationSpec,
        baseline: EvaluationRun,
        candidates: tuple[EvaluationRun, ...],
        comparisons: tuple[EvaluationComparison, ...],
    ) -> AblationResult:
        if baseline.eval_run_id != spec.baseline.eval_run_id:
            raise EvaluationComparisonError("ablation baseline differs")
        by_id = {item.eval_run_id: item for item in candidates}
        if {item.eval_run_id for item in spec.candidates} != set(by_id):
            raise EvaluationComparisonError("ablation candidate arms differ")
        all_runs = (baseline, *candidates)
        if any(
            item.dataset_hash != spec.dataset_hash
            or item.evaluation_policy_hash != spec.evaluation_policy_hash
            or item.case_ids != baseline.case_ids
            or item.evaluator_bundle_hash != baseline.evaluator_bundle_hash
            for item in all_runs
        ):
            raise IncompatibleEvaluationRuns("ablation controls differ")
        config_pin_id = ("run_config", "1")
        arms = (spec.baseline, *spec.candidates)
        for arm in arms:
            run = baseline if arm is spec.baseline else by_id[arm.eval_run_id]
            pin = next(
                (
                    item
                    for item in run.system.observations
                    if (item.pin_id, item.pin_version) == config_pin_id
                ),
                None,
            )
            if (
                pin is None
                or pin.status is not SUTPinStatus.ARTIFACT_VERIFIED
                or pin.value_hash != stable_hash(arm.declared_config_hash)
            ):
                raise EvaluationProvenanceError(
                    "ablation declared config hash differs"
                )
        comparison_by_candidate = {
            item.candidate_eval_run_id: item for item in comparisons
        }
        if set(comparison_by_candidate) != set(by_id) or any(
            item.baseline_eval_run_id != baseline.eval_run_id
            or item.baseline_semantic_hash != baseline.evaluation_semantic_hash
            or item.candidate_semantic_hash
            != by_id[item.candidate_eval_run_id].evaluation_semantic_hash
            for item in comparisons
        ):
            raise EvaluationComparisonError(
                "ablation comparisons do not bind the declared arms"
            )
        grade = baseline.provenance_grade
        for item in candidates:
            grade = weaker_grade(grade, item.provenance_grade)
        if spec.one_dimension_only and any(
            len(arm.changed_pin_ids) + len(arm.absent_by_design_pin_ids) != 1
            for arm in spec.candidates
        ):
            raise EvaluationProvenanceError(
                "one-dimensional ablation arm declares multiple changes"
            )
        verified_changes = True
        observed_change = False
        baseline_pins = {
            (item.pin_id, item.pin_version): item
            for item in baseline.system.observations
        }
        for arm in spec.candidates:
            candidate = by_id[arm.eval_run_id]
            candidate_pins = {
                (item.pin_id, item.pin_version): item
                for item in candidate.system.observations
            }
            changed_ids = set(arm.changed_pin_ids) | set(arm.absent_by_design_pin_ids)
            if not changed_ids:
                verified_changes = False
            keys = set(baseline_pins) | set(candidate_pins)
            for key in keys:
                left = baseline_pins.get(key)
                right = candidate_pins.get(key)
                if key[0] in {"researchos_commit", "system_version"}:
                    if (
                        left is None
                        or right is None
                        or left.value_hash != right.value_hash
                    ):
                        raise EvaluationProvenanceError(
                            f"ablation system identity differs: {key[0]}"
                        )
                    continue
                if left is None or right is None:
                    grade = weaker_grade(grade, ProvenanceGrade.PARTIALLY_VERIFIED)
                    verified_changes = False
                    continue
                is_changed = key[0] in changed_ids
                if is_changed:
                    verified_value_change = (
                        left.status is SUTPinStatus.ARTIFACT_VERIFIED
                        and right.status is SUTPinStatus.ARTIFACT_VERIFIED
                        and left.value_hash != right.value_hash
                    )
                    verified_absence = (
                        right.status is SUTPinStatus.ABSENT_BY_DESIGN
                        and key[0] in set(arm.absent_by_design_pin_ids)
                        and left.status is SUTPinStatus.ARTIFACT_VERIFIED
                    ) or (
                        left.status is SUTPinStatus.ABSENT_BY_DESIGN
                        and key[0]
                        in set(spec.baseline.absent_by_design_pin_ids)
                        and right.status is SUTPinStatus.ARTIFACT_VERIFIED
                    )
                    if verified_value_change or verified_absence:
                        observed_change = True
                    if not (verified_value_change or verified_absence):
                        verified_changes = False
                elif (
                    left.value_hash is not None
                    and right.value_hash is not None
                    and left.value_hash != right.value_hash
                ):
                    raise EvaluationProvenanceError(
                        f"undeclared ablation pin changed: {key[0]}"
                    )
                elif not (
                    (
                        left.status is SUTPinStatus.ARTIFACT_VERIFIED
                        and right.status is SUTPinStatus.ARTIFACT_VERIFIED
                    )
                    or (
                        left.status is SUTPinStatus.ABSENT_BY_DESIGN
                        and right.status is SUTPinStatus.ABSENT_BY_DESIGN
                        and left.supporting_ref.artifact_kind
                        is right.supporting_ref.artifact_kind
                    )
                ):
                    verified_changes = False
        causality = (
            AblationCausality.VERIFIED_CAUSAL_COMPARISON
            if verified_changes
            and observed_change
            else AblationCausality.PARTIALLY_VERIFIED_COMPARISON
            if grade is not ProvenanceGrade.DECLARED and observed_change
            else AblationCausality.DECLARED_ASSOCIATION
        )
        values = dict(
            ablation_id=spec.ablation_id,
            ablation_spec_hash=spec.ablation_spec_hash,
            ablation_spec=spec,
            comparisons=comparisons,
            causality=causality,
            provenance_grade=grade,
        )
        provisional = AblationResult.model_construct(
            **values, artifact_content_hash="0" * 64
        )
        return AblationResult(
            **values,
            artifact_content_hash=ablation_artifact_hash(provisional),
        )
