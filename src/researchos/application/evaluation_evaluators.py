"""Deterministic and exact-reference Phase 7 evaluators."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from researchos.application.citation_integrity import CitationIntegrityValidator
from researchos.application.errors import CorruptEvaluationInput
from researchos.application.evaluation_artifacts import FrozenRunArtifacts
from researchos.application.verification_coordinator import determine_disposition
from researchos.application.verification_publisher import is_publishable
from researchos.domain.claims import CitationSeverity, ClaimEvidenceRelation
from researchos.domain.contracts import model_sha256
from researchos.domain.evaluation import (
    REFERENCE_NORMALIZATION_VERSION,
    AggregationKind,
    ArtifactAvailability,
    ArtifactKind,
    BooleanMetricValue,
    EnumMetricValue,
    EvaluationCase,
    IntegerMetricValue,
    MetricCertainty,
    MetricDefinitionSnapshot,
    MetricDirection,
    MetricEvidenceProvenance,
    MetricEvidenceRef,
    MetricLayer,
    MetricStatus,
    MetricValue,
    MetricValueKind,
    NonComputedMetricValue,
    RatioMetricValue,
    normalize_metric_decimal_v1,
)
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.identity import normalize_content, sha256_text, stable_hash
from researchos.domain.runtime import TaskStatus
from researchos.domain.synthesis import JudgeVerdict


def _definition(
    metric_id: str,
    layer: MetricLayer,
    kind: MetricValueKind,
    unit: str,
    direction: MetricDirection,
    aggregation: AggregationKind,
    evaluator_id: str,
) -> MetricDefinitionSnapshot:
    values = dict(
        metric_id=metric_id,
        metric_version="1",
        layer=layer,
        value_kind=kind,
        unit=unit,
        direction=direction,
        evaluator_id=evaluator_id,
        evaluator_version="1",
        normalization_versions=(REFERENCE_NORMALIZATION_VERSION,),
        applicability_version="1",
        aggregation_kind=aggregation,
    )
    provisional = MetricDefinitionSnapshot.model_construct(
        **values, definition_hash="0" * 64
    )
    return MetricDefinitionSnapshot(
        **values,
        definition_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"definition_hash"})
        ),
    )


DETERMINISTIC_DEFINITIONS = (
    _definition(
        "planning_dag_valid",
        MetricLayer.PLANNING,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "deterministic_core",
    ),
    _definition(
        "planning_capability_feasible",
        MetricLayer.PLANNING,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "deterministic_core",
    ),
    _definition(
        "planning_task_count",
        MetricLayer.PLANNING,
        MetricValueKind.INTEGER,
        "count",
        MetricDirection.INFORMATIONAL,
        AggregationKind.NUMERIC,
        "deterministic_core",
    ),
    _definition(
        "execution_task_success_ratio",
        MetricLayer.EXECUTION,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "execution_blocked_task_ratio",
        MetricLayer.EXECUTION,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.LOWER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "execution_retry_count",
        MetricLayer.EXECUTION,
        MetricValueKind.INTEGER,
        "count",
        MetricDirection.LOWER_IS_BETTER,
        AggregationKind.NUMERIC,
        "deterministic_core",
    ),
    _definition(
        "execution_budget_adherent",
        MetricLayer.EXECUTION,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "deterministic_core",
    ),
    _definition(
        "evidence_claim_support_ratio",
        MetricLayer.EVIDENCE,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "evidence_unsupported_claim_ratio",
        MetricLayer.EVIDENCE,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.LOWER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "evidence_conflict_claim_ratio",
        MetricLayer.EVIDENCE,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.INFORMATIONAL,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "evidence_exact_duplicate_ratio",
        MetricLayer.EVIDENCE,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.LOWER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "citation_integrity_valid",
        MetricLayer.EVIDENCE,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "deterministic_core",
    ),
    _definition(
        "verification_publishable_claim_ratio",
        MetricLayer.VERIFICATION,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "verification_citation_completeness",
        MetricLayer.VERIFICATION,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "verification_unresolved_finding_ratio",
        MetricLayer.VERIFICATION,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.LOWER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "deterministic_core",
    ),
    _definition(
        "verification_disposition",
        MetricLayer.VERIFICATION,
        MetricValueKind.ENUM,
        "enum",
        MetricDirection.INFORMATIONAL,
        AggregationKind.ENUM_DISTRIBUTION,
        "deterministic_core",
    ),
    _definition(
        "verification_reported_consistency",
        MetricLayer.VERIFICATION,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "deterministic_core",
    ),
)

REFERENCE_DEFINITIONS = (
    _definition(
        "planning_reference_task_recall",
        MetricLayer.PLANNING,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "reference_exact",
    ),
    _definition(
        "planning_expected_capability_coverage",
        MetricLayer.PLANNING,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "reference_exact",
    ),
    _definition(
        "verification_expected_disposition_match",
        MetricLayer.VERIFICATION,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "reference_exact",
    ),
    _definition(
        "evidence_reference_claim_recall",
        MetricLayer.EVIDENCE,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "reference_exact",
    ),
    _definition(
        "evidence_reference_constraint_satisfaction",
        MetricLayer.EVIDENCE,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "reference_exact",
    ),
    _definition(
        "evidence_reference_citation_recall",
        MetricLayer.EVIDENCE,
        MetricValueKind.RATIO,
        "ratio",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.RATIO_MACRO_MICRO,
        "reference_exact",
    ),
)

ALL_METRIC_DEFINITIONS = tuple(
    sorted(
        (*DETERMINISTIC_DEFINITIONS, *REFERENCE_DEFINITIONS), key=lambda x: x.metric_id
    )
)
_DEFINITION_BY_ID = {item.metric_id: item for item in ALL_METRIC_DEFINITIONS}


def evaluator_bundle_hash(
    definitions: tuple[MetricDefinitionSnapshot, ...],
    *,
    model_bundle_hash: str | None = None,
) -> str:
    return stable_hash(
        {
            "definitions": [
                [item.evaluator_id, item.evaluator_version, item.definition_hash]
                for item in sorted(definitions, key=lambda value: value.metric_id)
            ],
            "model_bundle_hash": model_bundle_hash,
        }
    )


class DeterministicArtifactEvaluator:
    evaluator_id = "deterministic_core"
    evaluator_version = "1"
    definitions = DETERMINISTIC_DEFINITIONS

    def evaluate(
        self, case: EvaluationCase, frozen: FrozenRunArtifacts
    ) -> tuple[MetricValue, ...]:
        del case
        results: list[MetricValue] = []
        checkpoint = frozen.checkpoint
        if checkpoint is None:
            for metric_id in (
                "planning_dag_valid",
                "planning_capability_feasible",
                "planning_task_count",
                "execution_task_success_ratio",
                "execution_blocked_task_ratio",
                "execution_retry_count",
                "execution_budget_adherent",
            ):
                results.append(_missing(metric_id, "checkpoint_absent"))
        else:
            cp_ref = _evidence_ref(frozen, ArtifactKind.CHECKPOINT, "checkpoint")
            dag_valid = _recompute_dag_valid(checkpoint)
            results.extend(
                [
                    _boolean("planning_dag_valid", dag_valid, cp_ref),
                    _boolean(
                        "planning_capability_feasible",
                        all(
                            set(task.required_capability_ids).issubset(
                                frozen.run_state.config.allowed_capability_ids
                            )
                            for task in checkpoint.dag.tasks
                        ),
                        cp_ref,
                    ),
                    _integer("planning_task_count", len(checkpoint.dag.tasks), cp_ref),
                ]
            )
            states = checkpoint.task_states
            total = len(states)
            results.extend(
                [
                    _ratio_or_na(
                        "execution_task_success_ratio",
                        sum(item.status is TaskStatus.SUCCEEDED for item in states),
                        total,
                        cp_ref,
                    ),
                    _ratio_or_na(
                        "execution_blocked_task_ratio",
                        sum(item.status is TaskStatus.BLOCKED for item in states),
                        total,
                        cp_ref,
                    ),
                    _integer(
                        "execution_retry_count",
                        sum(max(0, len(item.attempts) - 1) for item in states),
                        cp_ref,
                    ),
                    _boolean(
                        "execution_budget_adherent",
                        not checkpoint.budget.breached
                        and all(
                            value >= 0
                            for value in checkpoint.budget.available()
                            .model_dump()
                            .values()
                        ),
                        cp_ref,
                    ),
                ]
            )
        results.extend(self._evidence_metrics(frozen))
        results.extend(self._verification_metrics(frozen))
        return tuple(sorted(results, key=lambda item: item.metric_id))

    def _evidence_metrics(self, frozen: FrozenRunArtifacts) -> list[MetricValue]:
        if frozen.evidence is None or frozen.claims is None:
            return [
                _missing(item, "claim_or_evidence_snapshot_absent")
                for item in (
                    "evidence_claim_support_ratio",
                    "evidence_unsupported_claim_ratio",
                    "evidence_conflict_claim_ratio",
                    "evidence_exact_duplicate_ratio",
                    "citation_integrity_valid",
                )
            ]
        ref = (
            _evidence_ref(frozen, ArtifactKind.CLAIMS, "current_entities_and_edges"),
            _evidence_ref(frozen, ArtifactKind.EVIDENCE, "current_entities"),
        )
        active_claims = {
            item.claim_id: item
            for item in frozen.claims.claims
            if item.lifecycle is RecordLifecycle.ACTIVE
        }
        relations = _current_valid_relations(frozen)
        supports = {
            claim_id
            for claim_id, _, relation in relations
            if relation is ClaimEvidenceRelation.SUPPORTS
        }
        contradicts = {
            claim_id
            for claim_id, _, relation in relations
            if relation is ClaimEvidenceRelation.CONTRADICTS
        }
        total = len(active_claims)
        active_evidence = [
            item
            for item in frozen.evidence.evidence
            if item.lifecycle is RecordLifecycle.ACTIVE
        ]
        revisions = {
            (item.evidence_id, item.revision): item
            for item in frozen.evidence.revisions
        }
        counts = Counter(
            revisions[(item.evidence_id, item.current_revision)].normalized_content_hash
            for item in active_evidence
        )
        duplicate_count = sum(value - 1 for value in counts.values() if value > 1)
        verification = frozen.verification
        citation_metric: MetricValue
        if verification is None:
            citation_metric = _missing(
                "citation_integrity_valid", "verification_absent"
            )
        else:
            integrity = CitationIntegrityValidator(
                claim_store=_StaticClaimStore(frozen.claims),
                evidence_store=_StaticEvidenceStore(frozen.evidence),
            ).validate(frozen.run_state.run_id, verification.final_draft.citations)
            citation_metric = _boolean(
                "citation_integrity_valid",
                not any(
                    item.severity is CitationSeverity.ERROR for item in integrity.issues
                ),
                (
                    _evidence_ref(
                        frozen, ArtifactKind.VERIFICATION, "final_draft.citations"
                    ),
                    *ref,
                ),
            )
        return [
            _ratio_or_na("evidence_claim_support_ratio", len(supports), total, ref),
            _ratio_or_na(
                "evidence_unsupported_claim_ratio", total - len(supports), total, ref
            ),
            _ratio_or_na(
                "evidence_conflict_claim_ratio", len(supports & contradicts), total, ref
            ),
            _ratio_or_na(
                "evidence_exact_duplicate_ratio",
                duplicate_count,
                len(active_evidence),
                ref,
            ),
            citation_metric,
        ]

    def _verification_metrics(self, frozen: FrozenRunArtifacts) -> list[MetricValue]:
        result = frozen.verification
        metric_ids = (
            "verification_publishable_claim_ratio",
            "verification_citation_completeness",
            "verification_unresolved_finding_ratio",
            "verification_disposition",
            "verification_reported_consistency",
        )
        if result is None or frozen.claims is None or frozen.evidence is None:
            return [
                _missing(item, "verification_or_snapshot_absent") for item in metric_ids
            ]
        ref = (
            _evidence_ref(frozen, ArtifactKind.VERIFICATION, "verification_result"),
            _evidence_ref(frozen, ArtifactKind.CLAIMS, "current_entities_and_edges"),
            _evidence_ref(frozen, ArtifactKind.EVIDENCE, "current_entities"),
        )
        integrity = CitationIntegrityValidator(
            claim_store=_StaticClaimStore(frozen.claims),
            evidence_store=_StaticEvidenceStore(frozen.evidence),
        ).validate(result.run_id, result.final_draft.citations)
        decisions = {
            item.report_claim_id: item.verdict for item in result.judge_decisions
        }
        resolved = set(result.resolved_finding_ids)
        blocked = {
            item.report_claim_id
            for item in result.findings
            if item.finding_id not in resolved
        }
        citation_error_claims = {
            item.claim_id
            for item in integrity.issues
            if item.severity is CitationSeverity.ERROR and item.claim_id is not None
        }
        global_error = any(
            item.severity is CitationSeverity.ERROR and item.claim_id is None
            for item in integrity.issues
        )
        publishable = [
            item
            for item in result.final_draft.report_claims
            if is_publishable(
                item.publication_state,
                decisions.get(item.report_claim_id, JudgeVerdict.UNRESOLVED),
                has_open_blocking_finding=item.report_claim_id in blocked,
                has_citation_error=global_error
                or item.claim_id in citation_error_claims,
            )
        ]
        current_supports = {
            (claim_id, evidence_id)
            for claim_id, evidence_id, relation in _current_valid_relations(frozen)
            if relation is ClaimEvidenceRelation.SUPPORTS
        }
        report_claim_by_claim = {
            item.claim_id: item for item in result.final_draft.report_claims
        }
        support_assignments = {
            report_claim_by_claim[citation.claim_id].report_claim_id
            for citation in result.final_draft.citations
            if citation.claim_id in report_claim_by_claim
            and (citation.claim_id, citation.evidence_id) in current_supports
        }
        reported_supports = {
            item.report_claim_id
            for item in result.citation_assignments
            if item.relation is ClaimEvidenceRelation.SUPPORTS
        }
        recomputed = determine_disposition(
            result.final_draft,
            result.judge_decisions,
            findings=result.findings,
            resolved_finding_ids=result.resolved_finding_ids,
            citation_issues=integrity.issues,
            omitted=bool(result.omitted_claim_ids or result.omitted_evidence_ids),
            open_findings=bool(blocked),
            pending_acquisition=bool(result.acquisition_requests),
            citation_errors=not integrity.valid,
        )
        return [
            _ratio_or_na(
                "verification_publishable_claim_ratio",
                len(publishable),
                len(result.final_draft.report_claims),
                ref,
            ),
            _ratio_or_na(
                "verification_citation_completeness",
                sum(
                    item.report_claim_id in support_assignments for item in publishable
                ),
                len(publishable),
                ref,
            ),
            _ratio_or_na(
                "verification_unresolved_finding_ratio",
                len(set(item.finding_id for item in result.findings) - resolved),
                len(set(item.finding_id for item in result.findings)),
                ref,
            ),
            _enum("verification_disposition", recomputed.value, ref),
            _boolean(
                "verification_reported_consistency",
                recomputed is result.disposition
                and tuple(integrity.issues) == tuple(result.citation_issues),
                # Persisted assignments are diagnostic only; they never establish
                # correctness, but disagreement must remain observable.
                ref,
            ).model_copy(
                update={
                    "value": recomputed is result.disposition
                    and tuple(integrity.issues) == tuple(result.citation_issues)
                    and reported_supports == support_assignments
                }
            ),
        ]


class ExactReferenceEvaluator:
    evaluator_id = "reference_exact"
    evaluator_version = "1"
    definitions = REFERENCE_DEFINITIONS

    def evaluate(
        self, case: EvaluationCase, frozen: FrozenRunArtifacts
    ) -> tuple[MetricValue, ...]:
        annotations = case.reference_annotations
        checkpoint = frozen.checkpoint
        ref = _evidence_ref(
            frozen,
            ArtifactKind.CHECKPOINT,
            "dag.tasks",
            allow_absent=True,
        )
        if not annotations.planning_tasks:
            task_recall = _na(
                "planning_reference_task_recall", "reference_not_annotated"
            )
        elif checkpoint is None:
            task_recall = _missing(
                "planning_reference_task_recall", "checkpoint_absent"
            )
        else:
            actual_hashes = sorted(
                (
                    sha256_text(normalize_content(item.objective)),
                    item.task_id,
                )
                for item in checkpoint.dag.tasks
            )
            required = sorted(
                (item for item in annotations.planning_tasks if item.required),
                key=lambda item: item.reference_task_id,
            )
            unused = set(range(len(actual_hashes)))
            matched = 0
            for expected in required:
                match = next(
                    (
                        index
                        for index, (content_hash, _) in enumerate(actual_hashes)
                        if index in unused
                        and content_hash == expected.normalized_objective_hash
                    ),
                    None,
                )
                if match is not None:
                    unused.remove(match)
                    matched += 1
            task_recall = _ratio_or_na(
                "planning_reference_task_recall",
                matched,
                len(required),
                ref,
            )
        if not annotations.expected_capability_ids:
            capability = _na(
                "planning_expected_capability_coverage", "reference_not_annotated"
            )
        elif checkpoint is None:
            capability = _missing(
                "planning_expected_capability_coverage", "checkpoint_absent"
            )
        else:
            actual = {
                value
                for task in checkpoint.dag.tasks
                for value in task.required_capability_ids
            }
            capability = _ratio_or_na(
                "planning_expected_capability_coverage",
                len(set(annotations.expected_capability_ids) & actual),
                len(annotations.expected_capability_ids),
                ref,
            )
        if not annotations.expected_dispositions:
            disposition = _na(
                "verification_expected_disposition_match", "reference_not_annotated"
            )
        elif frozen.verification is None:
            disposition = _missing(
                "verification_expected_disposition_match", "verification_absent"
            )
        else:
            recomputed = next(
                item
                for item in DeterministicArtifactEvaluator()._verification_metrics(
                    frozen
                )
                if item.metric_id == "verification_disposition"
            )
            disposition = _boolean(
                "verification_expected_disposition_match",
                isinstance(recomputed, EnumMetricValue)
                and recomputed.value
                in {item.value for item in annotations.expected_dispositions},
                _evidence_ref(
                    frozen, ArtifactKind.VERIFICATION, "recomputed_disposition"
                ),
            )
        claim_recall, evidence_satisfaction, citation_recall = (
            self._claim_evidence_reference_metrics(case, frozen)
        )
        return tuple(
            sorted(
                (
                    task_recall,
                    capability,
                    disposition,
                    claim_recall,
                    evidence_satisfaction,
                    citation_recall,
                ),
                key=lambda x: x.metric_id,
            )
        )

    def _claim_evidence_reference_metrics(
        self, case: EvaluationCase, frozen: FrozenRunArtifacts
    ) -> tuple[MetricValue, MetricValue, MetricValue]:
        annotations = case.reference_annotations
        metric_ids = (
            "evidence_reference_claim_recall",
            "evidence_reference_constraint_satisfaction",
            "evidence_reference_citation_recall",
        )
        if not annotations.claims:
            claim_metric = _na(metric_ids[0], "reference_not_annotated")
        elif frozen.claims is None:
            claim_metric = _missing(metric_ids[0], "claim_snapshot_absent")
        else:
            current = sorted(
                (revision.normalized_statement_hash, record.claim_id)
                for record in frozen.claims.claims
                if record.lifecycle is RecordLifecycle.ACTIVE
                for revision in frozen.claims.claim_revisions
                if revision.claim_id == record.claim_id
                and revision.revision == record.current_revision
            )
            required = sorted(
                (item for item in annotations.claims if item.required),
                key=lambda item: item.reference_claim_id,
            )
            unused = set(range(len(current)))
            matched = 0
            for expected in required:
                match = next(
                    (
                        index
                        for index, (content_hash, _) in enumerate(current)
                        if index in unused
                        and content_hash == expected.normalized_statement_hash
                    ),
                    None,
                )
                if match is not None:
                    unused.remove(match)
                    matched += 1
            claim_metric = _ratio_or_na(
                metric_ids[0],
                matched,
                len(required),
                _evidence_ref(frozen, ArtifactKind.CLAIMS, "current_claim_revisions"),
            )

        if not annotations.evidence:
            evidence_metric = _na(metric_ids[1], "reference_not_annotated")
        elif frozen.evidence is None or (
            frozen.claims is None
            and any(item.relation is not None for item in annotations.evidence)
        ):
            evidence_metric = _missing(metric_ids[1], "evidence_snapshot_absent")
        else:
            matches = _match_evidence_constraints(case, frozen)
            evidence_metric = _ratio_or_na(
                metric_ids[1],
                sum(
                    len(matches[item.constraint_id]) >= item.minimum_count
                    for item in annotations.evidence
                ),
                len(annotations.evidence),
                _evidence_ref(
                    frozen, ArtifactKind.EVIDENCE, "current_evidence_revisions"
                ),
            )

        if not annotations.citations:
            citation_metric = _na(metric_ids[2], "reference_not_annotated")
        elif frozen.claims is None or frozen.evidence is None:
            citation_metric = _missing(metric_ids[2], "claim_or_evidence_absent")
        else:
            claim_ids = {
                expected.reference_claim_id: record.claim_id
                for expected in annotations.claims
                for record in frozen.claims.claims
                if record.lifecycle is RecordLifecycle.ACTIVE
                for revision in frozen.claims.claim_revisions
                if revision.claim_id == record.claim_id
                and revision.revision == record.current_revision
                and revision.normalized_statement_hash
                == expected.normalized_statement_hash
            }
            evidence_matches = _match_evidence_constraints(case, frozen)
            actual = list(_current_valid_relations(frozen))
            unused = set(range(len(actual)))
            matched = 0
            for expected in sorted(
                annotations.citations,
                key=lambda item: (
                    item.reference_claim_id,
                    item.evidence_constraint_id,
                    item.relation.value,
                ),
            ):
                candidates = set(
                    evidence_matches.get(expected.evidence_constraint_id, ())
                )
                match = next(
                    (
                        index
                        for index, relation in enumerate(actual)
                        if index in unused
                        and relation
                        == (
                            claim_ids.get(expected.reference_claim_id),
                            relation[1],
                            expected.relation,
                        )
                        and relation[1] in candidates
                    ),
                    None,
                )
                if match is not None:
                    unused.remove(match)
                    matched += 1
            citation_metric = _ratio_or_na(
                metric_ids[2],
                matched,
                len(annotations.citations),
                _evidence_ref(frozen, ArtifactKind.CLAIMS, "current_valid_edges"),
            )
        return claim_metric, evidence_metric, citation_metric


def _match_evidence_constraints(
    case: EvaluationCase, frozen: FrozenRunArtifacts
) -> dict[str, tuple[str, ...]]:
    assert frozen.evidence is not None
    sources = {item.source_id: item for item in frozen.evidence.sources}
    revisions = {
        (item.evidence_id, item.revision): item for item in frozen.evidence.revisions
    }
    matches: dict[str, tuple[str, ...]] = {}
    used: set[str] = set()
    relations = (
        set(_current_valid_relations(frozen)) if frozen.claims is not None else set()
    )
    for constraint in sorted(
        case.reference_annotations.evidence, key=lambda item: item.constraint_id
    ):
        candidates = []
        for record in frozen.evidence.evidence:
            if record.lifecycle is not RecordLifecycle.ACTIVE:
                continue
            source = sources[record.source_id]
            revision = revisions[(record.evidence_id, record.current_revision)]
            if (
                constraint.source_type is not None
                and source.source_type.value != constraint.source_type
            ):
                continue
            if (
                constraint.canonical_locator_hash is not None
                and sha256_text(source.canonical_locator)
                != constraint.canonical_locator_hash
            ):
                continue
            if constraint.content_hashes and (
                revision.content_hash not in constraint.content_hashes
            ):
                continue
            if constraint.relation is not None and not any(
                evidence_id == record.evidence_id
                and relation is constraint.relation
                for _, evidence_id, relation in relations
            ):
                continue
            candidates.append(record.evidence_id)
        available = [item for item in sorted(candidates) if item not in used]
        selected = tuple(available[: constraint.minimum_count])
        used.update(selected)
        matches[constraint.constraint_id] = selected
    return matches


def _recompute_dag_valid(checkpoint) -> bool:
    dag = checkpoint.dag
    task_ids = [item.task_id for item in dag.tasks]
    if len(task_ids) != len(set(task_ids)) or set(task_ids) != set(
        dag.topological_order
    ):
        return False
    positions = {item: index for index, item in enumerate(dag.topological_order)}
    if any(
        dependency.task_id not in positions
        or positions[dependency.task_id] >= positions[task.task_id]
        for task in dag.tasks
        for dependency in task.dependencies
    ):
        return False
    return checkpoint.dag_hash == model_sha256(dag)


def _current_valid_relations(
    frozen: FrozenRunArtifacts,
) -> tuple[tuple[str, str, ClaimEvidenceRelation], ...]:
    assert frozen.claims is not None and frozen.evidence is not None
    claims = {item.claim_id: item for item in frozen.claims.claims}
    evidence = {item.evidence_id: item for item in frozen.evidence.evidence}
    claim_revisions = {
        (item.claim_id, item.revision) for item in frozen.claims.claim_revisions
    }
    evidence_revisions = {
        (item.evidence_id, item.revision) for item in frozen.evidence.revisions
    }
    revisions = {
        (item.edge_id, item.revision): item for item in frozen.claims.edge_revisions
    }
    result = []
    for edge in frozen.claims.edges:
        claim = claims.get(edge.claim_id)
        item = evidence.get(edge.evidence_id)
        revision = revisions.get((edge.edge_id, edge.current_revision))
        if claim is None or item is None or revision is None:
            raise CorruptEvaluationInput("claim evidence edge is dangling")
        if (claim.claim_id, claim.current_revision) not in claim_revisions:
            raise CorruptEvaluationInput("current claim revision is missing")
        if (item.evidence_id, item.current_revision) not in evidence_revisions:
            raise CorruptEvaluationInput("current evidence revision is missing")
        if (
            revision.claim_revision != claim.current_revision
            or revision.evidence_revision != item.current_revision
        ):
            raise CorruptEvaluationInput("claim evidence edge pins stale revisions")
        if (
            edge.lifecycle is RecordLifecycle.ACTIVE
            and claim.lifecycle is RecordLifecycle.ACTIVE
            and item.lifecycle is RecordLifecycle.ACTIVE
        ):
            result.append((edge.claim_id, edge.evidence_id, revision.relation))
    return tuple(sorted(result))


class _StaticClaimStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load(self, run_id):
        if self.snapshot.run_id != run_id:
            raise KeyError(run_id)
        return self.snapshot


class _StaticEvidenceStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load(self, run_id):
        if self.snapshot.run_id != run_id:
            raise KeyError(run_id)
        return self.snapshot


def _evidence_ref(
    frozen: FrozenRunArtifacts,
    kind: ArtifactKind,
    selector: str,
    *,
    allow_absent: bool = False,
) -> MetricEvidenceRef:
    item = next(
        value for value in frozen.manifest.artifacts if value.artifact_kind is kind
    )
    if item.availability is not ArtifactAvailability.PRESENT and not allow_absent:
        raise ValueError("metric evidence artifact is absent")
    return MetricEvidenceRef(
        artifact_kind=kind,
        input_manifest_hash=frozen.manifest.manifest_hash,
        artifact_sha256=item.sha256,
        field_selector=selector,
        provenance=MetricEvidenceProvenance.RECOMPUTED,
        algorithm_version="phase7-deterministic-v1",
    )


def _refs(
    value: MetricEvidenceRef | tuple[MetricEvidenceRef, ...],
) -> tuple[MetricEvidenceRef, ...]:
    return value if isinstance(value, tuple) else (value,)


def _boolean(
    metric_id: str,
    value: bool,
    ref: MetricEvidenceRef | tuple[MetricEvidenceRef, ...],
) -> BooleanMetricValue:
    return BooleanMetricValue(
        metric_id=metric_id,
        definition_hash=_DEFINITION_BY_ID[metric_id].definition_hash,
        value=value,
        certainty=MetricCertainty.EXACT,
        evidence_refs=_refs(ref),
    )


def _integer(
    metric_id: str,
    value: int,
    ref: MetricEvidenceRef | tuple[MetricEvidenceRef, ...],
) -> IntegerMetricValue:
    return IntegerMetricValue(
        metric_id=metric_id,
        definition_hash=_DEFINITION_BY_ID[metric_id].definition_hash,
        value=value,
        certainty=MetricCertainty.EXACT,
        evidence_refs=_refs(ref),
    )


def _enum(
    metric_id: str,
    value: str,
    ref: MetricEvidenceRef | tuple[MetricEvidenceRef, ...],
) -> EnumMetricValue:
    return EnumMetricValue(
        metric_id=metric_id,
        definition_hash=_DEFINITION_BY_ID[metric_id].definition_hash,
        value=value,
        certainty=MetricCertainty.EXACT,
        evidence_refs=_refs(ref),
    )


def _ratio_or_na(
    metric_id: str,
    numerator: int,
    denominator: int,
    ref: MetricEvidenceRef | tuple[MetricEvidenceRef, ...],
) -> MetricValue:
    if denominator == 0:
        return _na(metric_id, "zero_denominator")
    return RatioMetricValue(
        metric_id=metric_id,
        definition_hash=_DEFINITION_BY_ID[metric_id].definition_hash,
        value=normalize_metric_decimal_v1(Decimal(numerator) / denominator),
        numerator=numerator,
        denominator=denominator,
        certainty=MetricCertainty.EXACT,
        evidence_refs=_refs(ref),
    )


def _missing(metric_id: str, reason: str) -> NonComputedMetricValue:
    return NonComputedMetricValue(
        metric_id=metric_id,
        definition_hash=_DEFINITION_BY_ID[metric_id].definition_hash,
        status=MetricStatus.UNAVAILABLE,
        reason_code=reason,
    )


def _na(metric_id: str, reason: str) -> NonComputedMetricValue:
    return NonComputedMetricValue(
        metric_id=metric_id,
        definition_hash=_DEFINITION_BY_ID[metric_id].definition_hash,
        status=MetricStatus.NOT_APPLICABLE,
        reason_code=reason,
    )
