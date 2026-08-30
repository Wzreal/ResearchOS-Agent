from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from runtime_fixtures import build_executor, runtime_example
from test_phase6_synthesis_verification import build_service, prepared

from researchos.adapters.evaluation_dataset import InMemoryEvaluationDatasetLoader
from researchos.adapters.evaluation_filesystem import (
    FilesystemEvaluationArtifactStore,
)
from researchos.adapters.evaluation_memory import InMemoryEvaluationArtifactStore
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.adapters.mock_evaluation import MockEvaluationJudge
from researchos.adapters.mock_execution import MockTaskExecutionBackend
from researchos.adapters.mock_verification import MockVerificationModel
from researchos.adapters.verification_memory import InMemoryVerificationArtifactStore
from researchos.application.errors import (
    CorruptEvaluationArtifact,
    CorruptEvaluationInput,
    EvaluationComparisonError,
    EvaluationModelFailure,
    EvaluationPreconditionError,
    IncompatibleEvaluationRuns,
    MetricDefinitionCompatibilityError,
)
from researchos.application.evaluation_aggregation import aggregate_metrics
from researchos.application.evaluation_artifacts import (
    FrozenRunArtifacts,
    ReadOnlyRunArtifactReader,
)
from researchos.application.evaluation_comparison import (
    AblationService,
    EvaluationComparisonService,
)
from researchos.application.evaluation_evaluators import (
    DETERMINISTIC_DEFINITIONS,
    DeterministicArtifactEvaluator,
    ExactReferenceEvaluator,
    _definition,
)
from researchos.application.evaluation_harness import EvaluationHarness
from researchos.application.evaluation_identity import (
    evaluation_artifact_hash,
    evaluation_semantic_hash,
)
from researchos.application.run_manager import RunManager
from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.contracts import (
    RunConfig,
    RunInput,
    RunStatus,
    canonical_json_bytes,
    model_sha256,
)
from researchos.domain.evaluation import (
    AblationArm,
    AblationCausality,
    AblationSpec,
    AggregateStatistic,
    AggregationKind,
    ArtifactAvailability,
    ArtifactKind,
    ArtifactSnapshotRef,
    BooleanMetricValue,
    CaseRunBinding,
    DatasetProvenance,
    EvaluationCase,
    EvaluationDataset,
    EvaluationJudgeResponse,
    EvaluationPolicy,
    EvaluationRequest,
    EvaluationRunStatus,
    ExpectedCitationConstraint,
    ExpectedClaim,
    ExpectedEvidenceConstraint,
    MetricCertainty,
    MetricDirection,
    MetricEvidenceProvenance,
    MetricEvidenceRef,
    MetricLayer,
    MetricStatus,
    MetricValueKind,
    MissingMetricBehavior,
    ProvenanceGrade,
    ReferenceAnnotations,
    ReferenceLevel,
    RegressionPolicy,
    RegressionRule,
    RunArtifactCompleteness,
    RunArtifactManifest,
    SUTPinObservation,
    SUTPinStatus,
    SUTPinSupportKind,
    SUTPinSupportRef,
    ThresholdKind,
)
from researchos.domain.identity import sha256_text, stable_hash, stable_id
from researchos.domain.runtime import RuntimeResourceAmount


class NeverCancelled:
    cancelled = False

    async def wait(self):
        await asyncio.Future()


def _frozen(
    state,
    *,
    checkpoint=None,
    evidence=None,
    claims=None,
    verification=None,
) -> FrozenRunArtifacts:
    values = {
        ArtifactKind.RUN_STATE: state,
        ArtifactKind.CHECKPOINT: checkpoint,
        ArtifactKind.EVIDENCE: evidence,
        ArtifactKind.CLAIMS: claims,
        ArtifactKind.VERIFICATION: verification,
    }
    refs = []
    for kind in sorted(ArtifactKind, key=str):
        value = values.get(kind)
        refs.append(
            ArtifactSnapshotRef(
                artifact_kind=kind,
                relative_path=f"{state.run_id}/{kind.value}",
                availability=(
                    ArtifactAvailability.PRESENT
                    if value is not None
                    else ArtifactAvailability.ABSENT
                ),
                sha256=model_sha256(value) if value is not None else None,
                size_bytes=(
                    len(canonical_json_bytes(value)) if value is not None else None
                ),
                schema_version=1 if value is not None else None,
            )
        )
    values = dict(
        run_id=state.run_id,
        run_revision=state.revision,
        run_status=state.status.value,
        completeness=RunArtifactCompleteness.NONTERMINAL_SNAPSHOT,
        artifacts=tuple(refs),
    )
    provisional = RunArtifactManifest.model_construct(**values, manifest_hash="0" * 64)
    manifest = RunArtifactManifest(
        **values,
        manifest_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"manifest_hash"})
        ),
    )
    return FrozenRunArtifacts(
        manifest=manifest,
        run_state=state,
        checkpoint=checkpoint,
        trace=(),
        evidence=evidence,
        claims=claims,
        verification=verification,
        report_bytes=None,
    )


def _metric(metrics, metric_id):
    return next(item for item in metrics if item.metric_id == metric_id)


def _rehash_run(result, **updates):
    provisional = result.model_copy(
        update={
            **updates,
            "evaluation_semantic_hash": "0" * 64,
            "artifact_content_hash": "0" * 64,
        }
    )
    semantic = evaluation_semantic_hash(provisional)
    with_semantic = provisional.model_copy(
        update={"evaluation_semantic_hash": semantic}
    )
    return with_semantic.model_copy(
        update={"artifact_content_hash": evaluation_artifact_hash(with_semantic)}
    )


def _regression_policy(
    definition, *, relative=False, statistic=AggregateStatistic.MEAN
):
    values = dict(
        policy_id="regression_policy",
        policy_version="1",
        rules=(
            RegressionRule(
                metric_id=definition.metric_id,
                definition_hash=definition.definition_hash,
                threshold_kind=(
                    ThresholdKind.RELATIVE_DELTA
                    if relative
                    else ThresholdKind.ABSOLUTE_DELTA
                ),
                allowed_degradation=0.02,
                missing_behavior=MissingMetricBehavior.FAIL,
                aggregate_statistic=statistic,
            ),
        ),
    )
    provisional = RegressionPolicy.model_construct(**values, policy_hash="0" * 64)
    return RegressionPolicy(
        **values,
        policy_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"policy_hash"})
        ),
    )


def test_dag_validity_is_independently_recomputed():
    example = runtime_example()
    executor, _, _, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    evaluator = DeterministicArtifactEvaluator()
    metrics = evaluator.evaluate(None, _frozen(example.state, checkpoint=checkpoint))
    assert _metric(metrics, "planning_dag_valid").value is True

    reversed_dag = checkpoint.dag.model_copy(
        update={"topological_order": tuple(reversed(checkpoint.dag.topological_order))}
    )
    tampered = checkpoint.model_copy(
        update={"dag": reversed_dag, "dag_hash": model_sha256(reversed_dag)}
    )
    metrics = evaluator.evaluate(None, _frozen(example.state, checkpoint=tampered))
    assert _metric(metrics, "planning_dag_valid").value is False


def test_current_edge_and_citation_are_independently_recomputed():
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    service, _ = build_service(
        evidence_store,
        claim_store,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    verification = asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=10_000,
                cost_microunits=10_000,
                tool_calls=10,
            ),
            cancellation=NeverCancelled(),
        )
    )
    evidence = evidence_store.load(state.run_id)
    claims = claim_store.load(state.run_id)
    evaluator = DeterministicArtifactEvaluator()
    base = _frozen(state, evidence=evidence, claims=claims, verification=verification)
    assert (
        _metric(evaluator.evaluate(None, base), "evidence_claim_support_ratio").value
        == 1.0
    )
    base_metrics = evaluator.evaluate(None, base)
    forged_reported = verification.model_copy(update={"citation_assignments": ()})
    forged_metrics = evaluator.evaluate(
        None,
        _frozen(
            state,
            evidence=evidence,
            claims=claims,
            verification=forged_reported,
        ),
    )
    assert _metric(
        forged_metrics, "verification_citation_completeness"
    ).value == _metric(base_metrics, "verification_citation_completeness").value
    assert _metric(
        forged_metrics, "verification_reported_consistency"
    ).value is False

    edge_revision = claims.edge_revisions[0].model_copy(
        update={"claim_revision": claims.claims[0].current_revision + 1}
    )
    stale_claims = claims.model_copy(update={"edge_revisions": (edge_revision,)})
    with pytest.raises(CorruptEvaluationInput, match="stale revisions"):
        evaluator.evaluate(
            None,
            _frozen(
                state,
                evidence=evidence,
                claims=stale_claims,
                verification=verification,
            ),
        )

    citation = verification.final_draft.citations[0].model_copy(
        update={"evidence_revision": 999}
    )
    bad_draft = verification.final_draft.model_copy(update={"citations": (citation,)})
    bad_verification = verification.model_copy(update={"final_draft": bad_draft})
    citation_metrics = evaluator.evaluate(
        None,
        _frozen(
            state,
            evidence=evidence,
            claims=claims,
            verification=bad_verification,
        ),
    )
    assert _metric(citation_metrics, "citation_integrity_valid").value is False
    assert _metric(citation_metrics, "verification_reported_consistency").value is False


def test_reference_claim_evidence_and_citation_matching():
    state, _, _, evidence_store, claim_store, _ = prepared()
    evidence = evidence_store.load(state.run_id)
    claims = claim_store.load(state.run_id)
    claim_revision = claims.claim_revisions[0]
    evidence_revision = evidence.revisions[0]
    source = evidence.sources[0]
    case = EvaluationCase(
        case_id="case_reference",
        reference_level=ReferenceLevel.PARTIAL_GOLD,
        query="runtime query",
        reference_annotations=ReferenceAnnotations(
            claims=(
                ExpectedClaim(
                    reference_claim_id="reference_claim",
                    statement=claim_revision.statement,
                    normalized_statement_hash=claim_revision.normalized_statement_hash,
                ),
            ),
            evidence=(
                ExpectedEvidenceConstraint(
                    constraint_id="reference_evidence",
                    source_type=source.source_type.value,
                    canonical_locator_hash=sha256_text(source.canonical_locator),
                    content_hashes=(evidence_revision.content_hash,),
                ),
            ),
            citations=(
                ExpectedCitationConstraint(
                    reference_claim_id="reference_claim",
                    evidence_constraint_id="reference_evidence",
                    relation=ClaimEvidenceRelation.SUPPORTS,
                ),
            ),
        ),
    )
    metrics = ExactReferenceEvaluator().evaluate(
        case, _frozen(state, evidence=evidence, claims=claims)
    )
    assert _metric(metrics, "evidence_reference_claim_recall").value == 1.0
    assert _metric(metrics, "evidence_reference_constraint_satisfaction").value == 1.0
    assert _metric(metrics, "evidence_reference_citation_recall").value == 1.0


def test_reference_evidence_relation_and_matching_are_one_to_one():
    state, _, _, evidence_store, claim_store, _ = prepared()
    evidence = evidence_store.load(state.run_id)
    claims = claim_store.load(state.run_id)
    claim_revision = claims.claim_revisions[0]
    evidence_revision = evidence.revisions[0]
    source = evidence.sources[0]

    def evidence_constraint(constraint_id, *, relation=None):
        return ExpectedEvidenceConstraint(
            constraint_id=constraint_id,
            source_type=source.source_type.value,
            canonical_locator_hash=sha256_text(source.canonical_locator),
            content_hashes=(evidence_revision.content_hash,),
            relation=relation,
        )

    relation_mismatch = EvaluationCase(
        case_id="case_relation_mismatch",
        reference_level=ReferenceLevel.PARTIAL_GOLD,
        query="runtime query",
        reference_annotations=ReferenceAnnotations(
            evidence=(
                evidence_constraint(
                    "requires_contradiction",
                    relation=ClaimEvidenceRelation.CONTRADICTS,
                ),
            )
        ),
    )
    mismatch_metrics = ExactReferenceEvaluator().evaluate(
        relation_mismatch, _frozen(state, evidence=evidence, claims=claims)
    )
    assert (
        _metric(
            mismatch_metrics, "evidence_reference_constraint_satisfaction"
        ).value
        == 0.0
    )

    duplicate_evidence = EvaluationCase(
        case_id="case_duplicate_evidence",
        reference_level=ReferenceLevel.PARTIAL_GOLD,
        query="runtime query",
        reference_annotations=ReferenceAnnotations(
            evidence=(
                evidence_constraint("expected_evidence_a"),
                evidence_constraint("expected_evidence_b"),
            )
        ),
    )
    evidence_metrics = ExactReferenceEvaluator().evaluate(
        duplicate_evidence, _frozen(state, evidence=evidence, claims=claims)
    )
    assert (
        _metric(
            evidence_metrics, "evidence_reference_constraint_satisfaction"
        ).value
        == 0.5
    )

    duplicate_citations = EvaluationCase(
        case_id="case_duplicate_citations",
        reference_level=ReferenceLevel.PARTIAL_GOLD,
        query="runtime query",
        reference_annotations=ReferenceAnnotations(
            claims=(
                ExpectedClaim(
                    reference_claim_id="reference_claim",
                    statement=claim_revision.statement,
                    normalized_statement_hash=(
                        claim_revision.normalized_statement_hash
                    ),
                ),
            ),
            evidence=(evidence_constraint("reference_evidence"),),
            citations=(
                ExpectedCitationConstraint(
                    reference_claim_id="reference_claim",
                    evidence_constraint_id="reference_evidence",
                    relation=ClaimEvidenceRelation.SUPPORTS,
                ),
                ExpectedCitationConstraint(
                    reference_claim_id="reference_claim",
                    evidence_constraint_id="reference_evidence",
                    relation=ClaimEvidenceRelation.SUPPORTS,
                ),
            ),
        ),
    )
    citation_metrics = ExactReferenceEvaluator().evaluate(
        duplicate_citations, _frozen(state, evidence=evidence, claims=claims)
    )
    assert (
        _metric(citation_metrics, "evidence_reference_citation_recall").value
        == 0.5
    )


def test_aggregation_keeps_unavailable_separate_and_zero_denominator_is_na():
    definition = DETERMINISTIC_DEFINITIONS[0]
    ref = MetricEvidenceRef(
        artifact_kind=ArtifactKind.RUN_STATE,
        input_manifest_hash="0" * 64,
        artifact_sha256="0" * 64,
        field_selector="state",
        provenance=MetricEvidenceProvenance.RECOMPUTED,
        algorithm_version="1",
    )
    computed = BooleanMetricValue(
        metric_id=definition.metric_id,
        definition_hash=definition.definition_hash,
        value=False,
        certainty=MetricCertainty.EXACT,
        evidence_refs=(ref,),
    )
    from researchos.domain.evaluation import NonComputedMetricValue

    unavailable = NonComputedMetricValue(
        metric_id=definition.metric_id,
        definition_hash=definition.definition_hash,
        status=MetricStatus.UNAVAILABLE,
        reason_code="missing",
    )
    aggregate = aggregate_metrics(((computed,), (unavailable,)), (definition,))[0]
    assert aggregate.mean == 0.0
    assert aggregate.computed_count == 1
    assert aggregate.unavailable_count == 1


def test_filesystem_authority_round_trip_and_tamper(tmp_path):
    from test_phase7_harness import (
        Cancellation,
        build_harness,
        create_terminal_run,
        dataset,
        request,
    )

    from researchos.domain.evaluation import EvaluationCase, ReferenceLevel

    state, clock = create_terminal_run(tmp_path / "runs")
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data = dataset(case)
    memory = InMemoryEvaluationArtifactStore()
    _, _, harness = build_harness(tmp_path / "runs", case, clock)
    harness._artifacts = memory
    result = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    store = FilesystemEvaluationArtifactStore(tmp_path / "evaluations")
    store.publish(result)
    assert store.load(result.eval_run_id) == result
    authority = tmp_path / "evaluations" / result.eval_run_id / "evaluation.json"
    authority.write_bytes(
        authority.read_bytes().replace(b'"completed"', b'"partial"', 1)
    )
    with pytest.raises(CorruptEvaluationArtifact):
        store.load(result.eval_run_id)


def test_semantic_hash_ignores_timestamps_but_artifact_hash_does_not(tmp_path):
    from test_phase7_harness import (
        Cancellation,
        build_harness,
        create_terminal_run,
        request,
    )

    from researchos.domain.evaluation import EvaluationCase, ReferenceLevel

    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data, _, harness = build_harness(tmp_path, case, clock)
    result = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    shifted = result.model_copy(
        update={
            "started_at": result.started_at + timedelta(seconds=1),
            "completed_at": result.completed_at + timedelta(seconds=1),
        }
    )
    assert evaluation_semantic_hash(shifted) == result.evaluation_semantic_hash
    assert evaluation_artifact_hash(shifted) != result.artifact_content_hash


def test_comparison_checks_definition_and_threshold_direction(tmp_path):
    from test_phase7_harness import (
        Cancellation,
        build_harness,
        create_terminal_run,
        request,
    )

    from researchos.domain.evaluation import EvaluationCase, ReferenceLevel

    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data, _, harness = build_harness(tmp_path, case, clock)
    result = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    definition = next(
        item
        for item in result.metric_definitions
        if item.metric_id == "execution_task_success_ratio"
    )
    aggregate = next(
        item for item in result.aggregates if item.metric_id == definition.metric_id
    )
    baseline = _rehash_run(
        result,
        aggregates=tuple(
            item.model_copy(update={"mean": 0.90}) if item == aggregate else item
            for item in result.aggregates
        ),
    )
    candidate = _rehash_run(
        result,
        eval_run_id="eval_candidate",
        aggregates=tuple(
            item.model_copy(update={"mean": 0.85}) if item == aggregate else item
            for item in result.aggregates
        ),
    )
    comparison = EvaluationComparisonService().compare(
        baseline, candidate, _regression_policy(definition)
    )
    assert comparison.disposition.value == "fail"
    assert comparison.deltas[0].classification.value == "regression"

    relative_policy = _regression_policy(definition, relative=True)
    relative_regression = EvaluationComparisonService().compare(
        baseline, candidate, relative_policy
    )
    improved = _rehash_run(
        result,
        eval_run_id="eval_improved",
        aggregates=tuple(
            item.model_copy(update={"mean": 0.95}) if item == aggregate else item
            for item in result.aggregates
        ),
    )
    relative_improvement = EvaluationComparisonService().compare(
        baseline, improved, relative_policy
    )
    assert relative_regression.deltas[0].classification.value == "regression"
    assert relative_improvement.deltas[0].classification.value == "improvement"

    incompatible = _rehash_run(candidate, evaluator_bundle_hash="f" * 64)
    with pytest.raises(MetricDefinitionCompatibilityError):
        EvaluationComparisonService().compare(
            baseline, incompatible, _regression_policy(definition)
        )
    wrong_definition = definition.model_copy(update={"definition_hash": "e" * 64})
    wrong_definitions = tuple(
        wrong_definition if item.metric_id == definition.metric_id else item
        for item in candidate.metric_definitions
    )
    incompatible = _rehash_run(candidate, metric_definitions=wrong_definitions)
    with pytest.raises(MetricDefinitionCompatibilityError):
        EvaluationComparisonService().compare(
            baseline, incompatible, _regression_policy(definition)
        )


def test_target_metric_regression_rule_is_rejected_by_compare(tmp_path):
    from test_phase7_harness import (
        Cancellation,
        build_harness,
        create_terminal_run,
        request,
    )

    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_target",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data, _, harness = build_harness(tmp_path, case, clock)
    result = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    definition = next(
        item
        for item in result.metric_definitions
        if item.metric_id == "execution_task_success_ratio"
    )
    target_definition = _definition(
        definition.metric_id,
        definition.layer,
        definition.value_kind,
        definition.unit,
        MetricDirection.TARGET,
        definition.aggregation_kind,
        definition.evaluator_id,
    )
    target_definitions = tuple(
        target_definition if item.metric_id == definition.metric_id else item
        for item in result.metric_definitions
    )
    target_aggregates = tuple(
        item.model_copy(
            update={
                "definition_hash": target_definition.definition_hash,
                "mean": 0.5,
            }
        )
        if item.metric_id == definition.metric_id
        else item
        for item in result.aggregates
    )
    baseline = _rehash_run(
        result,
        metric_definitions=target_definitions,
        aggregates=target_aggregates,
    )
    candidate = _rehash_run(
        result,
        eval_run_id="eval_target_candidate",
        metric_definitions=target_definitions,
        aggregates=target_aggregates,
    )
    with pytest.raises(
        EvaluationComparisonError,
        match="regression rule requires a directional metric",
    ):
        EvaluationComparisonService().compare(
            baseline, candidate, _regression_policy(target_definition)
        )


def test_relative_comparison_with_zero_baseline_is_not_comparable(tmp_path):
    from test_phase7_harness import (
        Cancellation,
        build_harness,
        create_terminal_run,
        request,
    )

    from researchos.domain.evaluation import EvaluationCase, ReferenceLevel

    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data, _, harness = build_harness(tmp_path, case, clock)
    result = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    definition = next(
        item
        for item in result.metric_definitions
        if item.metric_id == "execution_retry_count"
    )
    aggregates = tuple(
        item.model_copy(update={"mean": 0.0})
        if item.metric_id == definition.metric_id
        else item
        for item in result.aggregates
    )
    baseline = _rehash_run(result, aggregates=aggregates)
    candidate = _rehash_run(
        result,
        eval_run_id="eval_candidate",
        aggregates=tuple(
            item.model_copy(update={"mean": 1.0})
            if item.metric_id == definition.metric_id
            else item
            for item in result.aggregates
        ),
    )
    comparison = EvaluationComparisonService().compare(
        baseline, candidate, _regression_policy(definition, relative=True)
    )
    assert comparison.disposition.value == "inconclusive"
    assert comparison.deltas[0].reason_code == "baseline_zero_relative_delta"


def test_declared_ablation_cannot_become_verified_causal(tmp_path):
    from test_phase7_harness import (
        Cancellation,
        build_harness,
        create_terminal_run,
        request,
    )

    from researchos.domain.evaluation import EvaluationCase, ReferenceLevel

    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data, _, harness = build_harness(tmp_path, case, clock)
    baseline = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    declared = SUTPinObservation(
        pin_id="retrieval_strategy",
        pin_version="1",
        value_hash=stable_hash("candidate"),
        status=SUTPinStatus.DECLARED,
        supporting_ref=SUTPinSupportRef(
            kind=SUTPinSupportKind.DECLARATION,
            input_manifest_hash=baseline.cases[0].input_manifest.manifest_hash,
        ),
    )
    system_values = baseline.system.model_dump(exclude={"system_hash"})
    system_values["observations"] = tuple(
        sorted((*baseline.system.observations, declared), key=lambda item: item.pin_id)
    )
    system_values["provenance_grade"] = ProvenanceGrade.PARTIALLY_VERIFIED
    system = baseline.system.model_construct(**system_values, system_hash="0" * 64)
    system = type(system)(
        **system_values,
        system_hash=stable_hash(
            system.model_dump(mode="json", exclude={"system_hash"})
        ),
    )
    candidate = _rehash_run(
        baseline,
        eval_run_id="eval_candidate",
        system=system,
        provenance_grade=ProvenanceGrade.PARTIALLY_VERIFIED,
    )
    spec_values = dict(
        dataset_hash=baseline.dataset_hash,
        evaluation_policy_hash=baseline.evaluation_policy_hash,
        baseline=AblationArm(
            arm_id="baseline",
            eval_run_id=baseline.eval_run_id,
            declared_config_hash=state.config_hash,
            changed_pin_ids=(),
        ),
        candidates=(
            AblationArm(
                arm_id="candidate",
                eval_run_id=candidate.eval_run_id,
                declared_config_hash=state.config_hash,
                changed_pin_ids=("retrieval_strategy",),
            ),
        ),
    )
    spec_hash = stable_hash(
        {
            **spec_values,
            "one_dimension_only": True,
            "baseline": spec_values["baseline"].model_dump(mode="json"),
            "candidates": [
                item.model_dump(mode="json") for item in spec_values["candidates"]
            ],
        }
    )
    spec = AblationSpec(
        **spec_values,
        ablation_id=stable_id("ablation", [spec_hash]),
        ablation_spec_hash=spec_hash,
    )
    definition = next(
        item
        for item in baseline.metric_definitions
        if item.metric_id == "execution_retry_count"
    )
    comparison = EvaluationComparisonService().compare(
        baseline, candidate, _regression_policy(definition)
    )
    mismatched = _rehash_run(candidate, case_ids=("other_case",))
    with pytest.raises(IncompatibleEvaluationRuns):
        AblationService().validate(
            spec, baseline, (mismatched,), (comparison,)
        )
    result = AblationService().validate(
        spec, baseline, (candidate,), (comparison,)
    )
    assert result.causality is not AblationCausality.VERIFIED_CAUSAL_COMPARISON


def test_mock_judge_is_exact_fixture_only_and_has_no_real_mode():
    with pytest.raises(EvaluationModelFailure, match="no REAL"):
        MockEvaluationJudge(model_bundle_hash="0" * 64, fixtures={}, mode="real")


def test_model_policy_requires_explicit_evaluator_and_strict_bounds():
    with pytest.raises(ValidationError):
        EvaluationPolicy(
            policy_id="policy",
            policy_version="1",
            enable_model_evaluators=True,
            max_model_calls=1,
            enabled_evaluator_ids=("deterministic_core",),
        )

    model_definition = _definition(
        "model_report_quality",
        MetricLayer.VERIFICATION,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "model_evaluator",
    )
    assert model_definition.evaluator_id == "model_evaluator"


def test_global_model_call_bound_and_missing_fixture_publish_no_authority(tmp_path):
    class Clock:
        current = datetime(2026, 8, 30, tzinfo=UTC)

        def now(self):
            return self.current

    class Ids:
        value = 0

        def __call__(self, prefix):
            self.value += 1
            return f"{prefix}_{self.value}"

    clock = Clock()
    manager = RunManager(
        store=FilesystemRunStore(tmp_path / "runs"),
        trace_sink=FilesystemTraceSink(tmp_path / "runs"),
        clock=clock,
        id_factory=Ids(),
    )

    def create(query):
        state = manager.create(
            RunInput(query=query), RunConfig(allowed_capability_ids=("search",))
        )
        for status in (
            RunStatus.PLANNING,
            RunStatus.READY,
            RunStatus.RUNNING,
            RunStatus.VERIFYING,
            RunStatus.EVALUATING,
        ):
            state = manager.transition(state.run_id, status)
        return manager.finalize(state.run_id, RunStatus.COMPLETED)

    states = (create("query a"), create("query b"))
    cases = tuple(
        EvaluationCase(
            case_id=f"case_{letter}",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query=f"query {letter}",
        )
        for letter in ("a", "b")
    )
    dataset_values = dict(
        dataset_id="dataset_model",
        dataset_version="1",
        provenance=DatasetProvenance(
            license_id="test",
            source_uri_hash="0" * 64,
            curator_id="tests",
            provenance_version="1",
        ),
        cases=cases,
    )
    provisional = EvaluationDataset.model_construct(
        **dataset_values, dataset_content_hash="0" * 64
    )
    dataset = EvaluationDataset(
        **dataset_values,
        dataset_content_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"dataset_content_hash"})
        ),
    )
    definition = _definition(
        "model_report_quality",
        MetricLayer.VERIFICATION,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "model_evaluator",
    )
    policy = EvaluationPolicy(
        policy_id="model_policy",
        policy_version="1",
        max_model_calls=1,
        enable_model_evaluators=True,
        enabled_evaluator_ids=("deterministic_core", "model_evaluator"),
    )
    request = EvaluationRequest(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_hash=dataset.dataset_content_hash,
        bindings=tuple(
            CaseRunBinding(case_id=case.case_id, run_id=state.run_id)
            for case, state in zip(cases, states, strict=True)
        ),
        commit_sha="a" * 40,
        system_version="1",
        policy=policy,
        deadline=clock.now() + timedelta(minutes=1),
    )
    bundle_hash = stable_hash("mock-model")
    probe = MockEvaluationJudge(model_bundle_hash=bundle_hash, fixtures={})
    probe_store = InMemoryEvaluationArtifactStore()
    probe_harness = EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((dataset,)),
        artifacts=probe_store,
        run_artifacts=ReadOnlyRunArtifactReader(
            tmp_path / "runs", max_input_bytes_per_case=1_000_000
        ),
        clock=clock,
        evaluators=(DeterministicArtifactEvaluator(),),
        judge=probe,
        model_definitions=(definition,),
    )
    with pytest.raises(EvaluationPreconditionError, match="global model call"):
        asyncio.run(probe_harness.evaluate_existing(request, NeverCancelled()))
    assert not probe_store._runs
    assert probe.calls == 0

    single_request = request.model_copy(update={"bindings": request.bindings[:1]})
    single_probe = MockEvaluationJudge(model_bundle_hash=bundle_hash, fixtures={})
    single_probe_harness = EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((dataset,)),
        artifacts=InMemoryEvaluationArtifactStore(),
        run_artifacts=ReadOnlyRunArtifactReader(
            tmp_path / "runs", max_input_bytes_per_case=1_000_000
        ),
        clock=clock,
        evaluators=(DeterministicArtifactEvaluator(),),
        judge=single_probe,
        model_definitions=(definition,),
    )
    partial = asyncio.run(
        single_probe_harness.evaluate_existing(single_request, NeverCancelled())
    )
    assert partial.status is EvaluationRunStatus.PARTIAL
    single_model_request = single_probe.requests[0]
    single_fixture = EvaluationJudgeResponse(
        fixture_key=single_model_request.fixture_key,
        evaluator_id=single_model_request.evaluator_id,
        evaluator_version=single_model_request.evaluator_version,
        model_id="mock_judge",
        model_bundle_hash=bundle_hash,
        raw_response_bytes=100,
        metrics=(
            BooleanMetricValue(
                metric_id=definition.metric_id,
                definition_hash=definition.definition_hash,
                value=True,
                certainty=MetricCertainty.MODEL_ASSESSED,
                evidence_refs=(
                    MetricEvidenceRef(
                        artifact_kind=ArtifactKind.RUN_STATE,
                        input_manifest_hash=single_model_request.context[
                            "manifest_hash"
                        ],
                        field_selector="model_context",
                        provenance=MetricEvidenceProvenance.AUTHORITATIVE,
                        algorithm_version="model-evaluator-v1",
                    ),
                ),
            ),
        ),
    )
    replay_judge = MockEvaluationJudge(
        model_bundle_hash=bundle_hash,
        fixtures={single_model_request.fixture_key: single_fixture},
    )
    replay_store = InMemoryEvaluationArtifactStore()
    replay_harness = EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((dataset,)),
        artifacts=replay_store,
        run_artifacts=ReadOnlyRunArtifactReader(
            tmp_path / "runs", max_input_bytes_per_case=1_000_000
        ),
        clock=clock,
        evaluators=(DeterministicArtifactEvaluator(),),
        judge=replay_judge,
        model_definitions=(definition,),
    )
    first = asyncio.run(
        replay_harness.evaluate_existing(single_request, NeverCancelled())
    )
    second = asyncio.run(
        replay_harness.evaluate_existing(single_request, NeverCancelled())
    )
    assert second == first
    assert replay_judge.calls == 1
    assert first.evaluation_model_bundle_hash == bundle_hash
