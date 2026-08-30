from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from pydantic import ValidationError
from runtime_fixtures import build_executor, runtime_example
from test_phase6_synthesis_verification import prepared
from test_phase7_harness import Cancellation, Clock, Ids, dataset, request
from test_phase7_semantic_audit import _frozen, _regression_policy

from researchos.adapters.checkpoint_filesystem import FilesystemCheckpointStore
from researchos.adapters.evaluation_dataset import InMemoryEvaluationDatasetLoader
from researchos.adapters.evaluation_filesystem import FilesystemEvaluationArtifactStore
from researchos.adapters.evaluation_memory import InMemoryEvaluationArtifactStore
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.adapters.mock_execution import MockTaskExecutionBackend
from researchos.application.errors import (
    CorruptEvaluationArtifact,
    CorruptEvaluationInput,
    EvaluationArtifactConflict,
    EvaluationCancelled,
    EvaluationInputChanged,
    EvaluationPersistenceError,
    EvaluationPreconditionError,
    EvaluationProvenanceError,
    SUTHomogeneityError,
)
from researchos.application.evaluation_artifacts import ReadOnlyRunArtifactReader
from researchos.application.evaluation_comparison import (
    AblationService,
    EvaluationComparisonService,
)
from researchos.application.evaluation_compatibility import (
    observe_sut,
    validate_sut_homogeneity,
)
from researchos.application.evaluation_evaluators import (
    DeterministicArtifactEvaluator,
    ExactReferenceEvaluator,
    _definition,
)
from researchos.application.evaluation_harness import EvaluationHarness
from researchos.application.evaluation_identity import (
    ablation_artifact_hash,
    case_artifact_hash,
    comparison_artifact_hash,
    evaluation_artifact_hash,
)
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import RunConfig, RunInput, RunStatus
from researchos.domain.evaluation import (
    AblationArm,
    AblationCausality,
    AblationSpec,
    AggregationKind,
    ArtifactAvailability,
    ArtifactKind,
    BooleanMetricValue,
    ComparisonDisposition,
    DatasetProvenance,
    EvaluationCase,
    EvaluationCaseStatus,
    EvaluationDataset,
    EvaluationJudgeResponse,
    EvaluationPolicy,
    EvaluationRequest,
    EvaluationRun,
    EvaluationRunStatus,
    ExpectedClaim,
    ExpectedPlanningTask,
    MetricCertainty,
    MetricDirection,
    MetricEvidenceProvenance,
    MetricEvidenceRef,
    MetricLayer,
    MetricStatus,
    MetricValueKind,
    ProvenanceGrade,
    ReferenceAnnotations,
    ReferenceLevel,
    RegressionRule,
    SUTPinObservation,
    SUTPinStatus,
    SUTPinSupportKind,
    SUTPinSupportRef,
    ThresholdKind,
    normalize_metric_decimal_v1,
)
from researchos.domain.identity import (
    normalize_content,
    sha256_text,
    stable_hash,
    stable_id,
)


def _terminal(manager: RunManager, query: str, capabilities=("search",)):
    state = manager.create(
        RunInput(query=query),
        RunConfig(allowed_capability_ids=capabilities),
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


def _dataset(*cases: EvaluationCase) -> EvaluationDataset:
    values = dict(
        dataset_id="dataset_hardening",
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
        **values, dataset_content_hash="0" * 64
    )
    return EvaluationDataset(
        **values,
        dataset_content_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"dataset_content_hash"})
        ),
    )


def _harness(root, data, clock, store=None, evaluators=None):
    store = store or InMemoryEvaluationArtifactStore()
    return store, EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((data,)),
        artifacts=store,
        run_artifacts=ReadOnlyRunArtifactReader(
            root, max_input_bytes_per_case=1_000_000
        ),
        clock=clock,
        evaluators=evaluators
        or (DeterministicArtifactEvaluator(), ExactReferenceEvaluator()),
    )


def _request(data, bindings, clock, *, policy=None):
    return EvaluationRequest(
        dataset_id=data.dataset_id,
        dataset_version=data.dataset_version,
        dataset_hash=data.dataset_content_hash,
        bindings=bindings,
        commit_sha="a" * 40,
        system_version="1",
        policy=policy
        or EvaluationPolicy(
            policy_id="hardening_policy",
            policy_version="1",
            enabled_evaluator_ids=("deterministic_core", "reference_exact"),
        ),
        deadline=clock.now() + timedelta(minutes=1),
    )


def _rehash_evaluation(result: EvaluationRun, **updates) -> EvaluationRun:
    provisional = result.model_copy(
        update={**updates, "artifact_content_hash": "0" * 64}
    )
    return provisional.model_copy(
        update={"artifact_content_hash": evaluation_artifact_hash(provisional)}
    )


def _spec(
    baseline,
    candidate,
    baseline_config,
    candidate_config,
    *,
    changed_pin_ids=("run_config",),
    one_dimension_only=True,
):
    values = dict(
        dataset_hash=baseline.dataset_hash,
        evaluation_policy_hash=baseline.evaluation_policy_hash,
        baseline=AblationArm(
            arm_id="baseline",
            eval_run_id=baseline.eval_run_id,
            declared_config_hash=baseline_config,
            changed_pin_ids=(),
        ),
        candidates=(
            AblationArm(
                arm_id="candidate",
                eval_run_id=candidate.eval_run_id,
                declared_config_hash=candidate_config,
                changed_pin_ids=changed_pin_ids,
            ),
        ),
        one_dimension_only=one_dimension_only,
    )
    serialized = {
        **values,
        "baseline": values["baseline"].model_dump(mode="json"),
        "candidates": [item.model_dump(mode="json") for item in values["candidates"]],
    }
    content_hash = stable_hash(serialized)
    return AblationSpec(
        **values,
        ablation_id=stable_id("ablation", [content_hash]),
        ablation_spec_hash=content_hash,
    )


def test_corrupt_checkpoint_preserves_raw_identity_and_case_fails(tmp_path):
    clock = Clock()
    manager = RunManager(
        store=FilesystemRunStore(tmp_path),
        trace_sink=FilesystemTraceSink(tmp_path),
        clock=clock,
        id_factory=Ids(),
    )
    state = _terminal(manager, "query")
    payload = b'{"checkpoint":"truncated"'
    (tmp_path / state.run_id / "checkpoint.json").write_bytes(payload)
    reader = ReadOnlyRunArtifactReader(tmp_path, max_input_bytes_per_case=1_000_000)
    frozen = reader.freeze(state.run_id)
    checkpoint = next(
        item
        for item in frozen.manifest.artifacts
        if item.artifact_kind is ArtifactKind.CHECKPOINT
    )
    assert checkpoint.availability is ArtifactAvailability.CORRUPT
    assert checkpoint.sha256 == __import__("hashlib").sha256(payload).hexdigest()
    assert checkpoint.size_bytes == len(payload)

    case = EvaluationCase(
        case_id="case_a", reference_level=ReferenceLevel.STRUCTURAL_ONLY, query="query"
    )
    data = dataset(case)
    store, harness = _harness(tmp_path, data, clock)
    value = request(data, state.run_id, clock)
    value = value.model_copy(
        update={
            "bindings": (
                value.bindings[0].model_copy(
                    update={"absent_by_design_pin_ids": ("planner_bundle",)}
                ),
            )
        }
    )
    result = asyncio.run(
        harness.evaluate_existing(value, Cancellation())
    )
    assert result.status is EvaluationRunStatus.FAILED
    assert result.cases[0].status is EvaluationCaseStatus.FAILED
    assert not any(
        item.status is SUTPinStatus.ABSENT_BY_DESIGN
        for item in result.cases[0].sut_observations
    )
    assert store.load(result.eval_run_id) == result


def test_mixed_corrupt_and_healthy_cases_are_partial(tmp_path):
    clock = Clock()
    manager = RunManager(
        store=FilesystemRunStore(tmp_path),
        trace_sink=FilesystemTraceSink(tmp_path),
        clock=clock,
        id_factory=Ids(),
    )
    bad = _terminal(manager, "bad query")
    good = _terminal(manager, "good query")
    (tmp_path / bad.run_id / "checkpoint.json").write_bytes(b"bad")
    cases = (
        EvaluationCase(
            case_id="case_bad",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="bad query",
        ),
        EvaluationCase(
            case_id="case_good",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="good query",
        ),
    )
    data = _dataset(*cases)
    _, harness = _harness(tmp_path, data, clock)
    value = _request(
        data,
        (
            {"case_id": "case_bad", "run_id": bad.run_id},
            {"case_id": "case_good", "run_id": good.run_id},
        ),
        clock,
    )
    result = asyncio.run(harness.evaluate_existing(value, Cancellation()))
    assert result.status is EvaluationRunStatus.PARTIAL
    assert [item.status for item in result.cases] == [
        EvaluationCaseStatus.FAILED,
        EvaluationCaseStatus.COMPLETED,
    ]


def test_reader_bound_is_checked_before_open_and_symlink_fails(tmp_path, monkeypatch):
    state, _ = __import__("test_phase7_harness").create_terminal_run(tmp_path)
    reader = ReadOnlyRunArtifactReader(tmp_path, max_input_bytes_per_case=1_000_000)
    opened = False
    original = __import__("pathlib").Path.open

    def probe(path, *args, **kwargs):
        nonlocal opened
        opened = True
        return original(path, *args, **kwargs)

    monkeypatch.setattr(__import__("pathlib").Path, "open", probe)
    with pytest.raises(EvaluationPreconditionError, match="byte bound"):
        reader.freeze(state.run_id, max_input_bytes=1)
    assert not opened

    monkeypatch.undo()
    link = tmp_path / state.run_id / "checkpoint.json"
    original_is_symlink = __import__("pathlib").Path.is_symlink
    monkeypatch.setattr(
        __import__("pathlib").Path,
        "is_symlink",
        lambda path: path == link or original_is_symlink(path),
    )
    with pytest.raises(CorruptEvaluationInput, match="symlink"):
        reader.freeze(state.run_id)


@pytest.mark.parametrize("restore_original", (False, True))
def test_frozen_typed_state_comes_from_the_same_raw_snapshot(
    tmp_path, restore_original
):
    state, _ = __import__("test_phase7_harness").create_terminal_run(tmp_path)
    state_path = tmp_path / state.run_id / "run_state.json"
    original = state_path.read_bytes()

    class MutatingReader(ReadOnlyRunArtifactReader):
        def _decode_run_state(self, data, run_id):
            state_path.write_bytes(b"{}")
            result = super()._decode_run_state(data, run_id)
            if restore_original:
                state_path.write_bytes(original)
            return result

    reader = MutatingReader(tmp_path, max_input_bytes_per_case=1_000_000)
    frozen = reader.freeze(state.run_id)
    state_ref = next(
        item
        for item in frozen.manifest.artifacts
        if item.artifact_kind is ArtifactKind.RUN_STATE
    )
    assert frozen.run_state == state
    assert state_ref.sha256 == __import__("hashlib").sha256(original).hexdigest()
    if restore_original:
        assert reader.fingerprint(state.run_id) == frozen.raw_fingerprint
    else:
        assert reader.fingerprint(state.run_id) != frozen.raw_fingerprint


def test_sut_pin_trust_and_version_conflict(tmp_path):
    with pytest.raises(ValidationError):
        SUTPinObservation(
            pin_id="fake",
            pin_version="1",
            value_hash="0" * 64,
            status=SUTPinStatus.ARTIFACT_VERIFIED,
            supporting_ref=SUTPinSupportRef(
                kind=SUTPinSupportKind.DECLARATION,
                input_manifest_hash="0" * 64,
            ),
        )
    declared = SUTPinObservation(
        pin_id="pin",
        pin_version="1",
        value_hash=stable_hash("a"),
        status=SUTPinStatus.DECLARED,
        supporting_ref=SUTPinSupportRef(
            kind=SUTPinSupportKind.DECLARATION,
            input_manifest_hash="0" * 64,
        ),
    )
    with pytest.raises(SUTHomogeneityError, match="version conflict"):
        validate_sut_homogeneity(
            commit_sha="a" * 40,
            system_version="1",
            observations_by_case=(
                (declared,),
                (declared.model_copy(update={"pin_version": "2"}),),
            ),
        )

    state, _ = __import__("test_phase7_harness").create_terminal_run(tmp_path)
    frozen = ReadOnlyRunArtifactReader(
        tmp_path, max_input_bytes_per_case=1_000_000
    ).freeze(state.run_id)
    observations = {item.pin_id: item for item in observe_sut(frozen)}
    assert observations["planner_bundle"].status is SUTPinStatus.UNOBSERVABLE
    assert observations["runtime_schema"].status is SUTPinStatus.UNOBSERVABLE


def test_duplicate_references_cannot_reuse_one_actual_task_or_claim():
    example = runtime_example()
    executor, _, _, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    objective = checkpoint.dag.tasks[0].objective
    tasks = tuple(
        ExpectedPlanningTask(
            reference_task_id=f"reference_task_{index}",
            objective=objective,
            normalized_objective_hash=sha256_text(normalize_content(objective)),
        )
        for index in range(2)
    )
    task_case = EvaluationCase(
        case_id="case_tasks",
        reference_level=ReferenceLevel.PARTIAL_GOLD,
        query="query",
        reference_annotations=ReferenceAnnotations(planning_tasks=tasks),
    )
    task_metrics = ExactReferenceEvaluator().evaluate(
        task_case, _frozen(example.state, checkpoint=checkpoint)
    )
    task_recall = next(
        item
        for item in task_metrics
        if item.metric_id == "planning_reference_task_recall"
    )
    assert task_recall.value == 0.5

    state, _, _, evidence_store, claim_store, _ = prepared()
    claims = claim_store.load(state.run_id)
    evidence = evidence_store.load(state.run_id)
    revision = claims.claim_revisions[0]
    expected_claims = tuple(
        ExpectedClaim(
            reference_claim_id=f"reference_claim_{index}",
            statement=revision.statement,
            normalized_statement_hash=revision.normalized_statement_hash,
        )
        for index in range(2)
    )
    claim_case = EvaluationCase(
        case_id="case_claims",
        reference_level=ReferenceLevel.PARTIAL_GOLD,
        query="query",
        reference_annotations=ReferenceAnnotations(claims=expected_claims),
    )
    claim_metrics = ExactReferenceEvaluator().evaluate(
        claim_case, _frozen(state, evidence=evidence, claims=claims)
    )
    claim_recall = next(
        item
        for item in claim_metrics
        if item.metric_id == "evidence_reference_claim_recall"
    )
    assert claim_recall.value == 0.5


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.2345678901245, 1.234567890124),
        (1.2345678901255, 1.234567890126),
        (-1.2345678901245, -1.234567890124),
    ],
)
def test_metric_half_even_normalization(value, expected):
    assert normalize_metric_decimal_v1(value) == expected


def test_metric_count_preflight_and_cancellation_between_evaluators(tmp_path):
    state, clock = __import__("test_phase7_harness").create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a", reference_level=ReferenceLevel.STRUCTURAL_ONLY, query="query"
    )
    data = dataset(case)

    class Spy:
        evaluator_id = "deterministic_core"
        evaluator_version = "1"
        definitions = DeterministicArtifactEvaluator.definitions
        calls = 0

        def evaluate(self, case, frozen):
            self.calls += 1
            return DeterministicArtifactEvaluator().evaluate(case, frozen)

    spy = Spy()
    _, harness = _harness(tmp_path, data, clock, evaluators=(spy,))
    policy = EvaluationPolicy(
        policy_id="small",
        policy_version="1",
        max_metrics_per_case=1,
        enabled_evaluator_ids=("deterministic_core",),
    )
    with pytest.raises(EvaluationPreconditionError, match="metric count"):
        asyncio.run(
            harness.evaluate_existing(
                _request(
                    data,
                    ({"case_id": "case_a", "run_id": state.run_id},),
                    clock,
                    policy=policy,
                ),
                Cancellation(),
            )
        )
    assert spy.calls == 0

    class Switch(Cancellation):
        pass

    signal = Switch()

    class First:
        evaluator_id = "deterministic_core"
        evaluator_version = "1"
        definitions = DeterministicArtifactEvaluator.definitions

        def evaluate(self, case, frozen):
            result = DeterministicArtifactEvaluator().evaluate(case, frozen)
            signal._cancelled = True
            return result

    class Second:
        evaluator_id = "reference_exact"
        evaluator_version = "1"
        definitions = ExactReferenceEvaluator.definitions
        calls = 0

        def evaluate(self, case, frozen):
            self.calls += 1
            return ExactReferenceEvaluator().evaluate(case, frozen)

    second = Second()
    store, harness = _harness(tmp_path, data, clock, evaluators=(First(), second))
    with pytest.raises(EvaluationCancelled):
        asyncio.run(
            harness.evaluate_existing(request(data, state.run_id, clock), signal)
        )
    assert second.calls == 0
    assert not store._runs


@pytest.mark.parametrize(
    "wrong_field",
    (
        "fixture_key",
        "evaluator_id",
        "evaluator_version",
        "model_bundle_hash",
        "metric_id",
        "definition_hash",
    ),
)
def test_wrong_judge_identity_is_case_local_error(tmp_path, wrong_field):
    state, clock = __import__("test_phase7_harness").create_terminal_run(tmp_path)
    data = dataset(
        EvaluationCase(
            case_id="case_a",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="query",
        )
    )
    definition = _definition(
        "model_quality",
        MetricLayer.VERIFICATION,
        MetricValueKind.BOOLEAN,
        "boolean",
        MetricDirection.HIGHER_IS_BETTER,
        AggregationKind.BOOLEAN_RATE,
        "model_evaluator",
    )
    bundle_hash = stable_hash("model")

    class Judge:
        model_bundle_hash = bundle_hash

        async def evaluate(self, value, cancellation):
            del cancellation
            response = EvaluationJudgeResponse(
                fixture_key=value.fixture_key,
                evaluator_id=value.evaluator_id,
                evaluator_version=value.evaluator_version,
                model_id="mock_judge",
                model_bundle_hash=bundle_hash,
                raw_response_bytes=10,
                metrics=(
                    BooleanMetricValue(
                        metric_id=definition.metric_id,
                        definition_hash=definition.definition_hash,
                        value=True,
                        certainty=MetricCertainty.MODEL_ASSESSED,
                        evidence_refs=(
                            MetricEvidenceRef(
                                artifact_kind=ArtifactKind.RUN_STATE,
                                input_manifest_hash=value.context["manifest_hash"],
                                field_selector="model_context",
                                provenance=MetricEvidenceProvenance.AUTHORITATIVE,
                                algorithm_version="model-evaluator-v1",
                            ),
                        ),
                    ),
                ),
            )
            replacements = {
                "fixture_key": "0" * 64,
                "evaluator_id": "wrong_evaluator",
                "evaluator_version": "2",
                "model_bundle_hash": "1" * 64,
            }
            if wrong_field in {"metric_id", "definition_hash"}:
                metric_replacements = {
                    "metric_id": "wrong_model_quality",
                    "definition_hash": "2" * 64,
                }
                return response.model_copy(
                    update={
                        "metrics": (
                            response.metrics[0].model_copy(
                                update={
                                    wrong_field: metric_replacements[wrong_field]
                                }
                            ),
                        )
                    }
                )
            return response.model_copy(
                update={wrong_field: replacements[wrong_field]}
            )

    policy = EvaluationPolicy(
        policy_id="model_policy",
        policy_version="1",
        max_model_calls=1,
        enable_model_evaluators=True,
        enabled_evaluator_ids=("deterministic_core", "model_evaluator"),
    )
    store = InMemoryEvaluationArtifactStore()
    harness = EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((data,)),
        artifacts=store,
        run_artifacts=ReadOnlyRunArtifactReader(
            tmp_path, max_input_bytes_per_case=1_000_000
        ),
        clock=clock,
        evaluators=(DeterministicArtifactEvaluator(),),
        judge=Judge(),
        model_definitions=(definition,),
    )
    result = asyncio.run(
        harness.evaluate_existing(
            _request(
                data,
                ({"case_id": "case_a", "run_id": state.run_id},),
                clock,
                policy=policy,
            ),
            Cancellation(),
        )
    )
    assert result.status is EvaluationRunStatus.PARTIAL
    metric = next(
        item for item in result.cases[0].metrics if item.metric_id == "model_quality"
    )
    assert metric.status is MetricStatus.ERROR
    assert not any(
        item.metric_id == "wrong_model_quality"
        for item in result.cases[0].metrics
    )


def test_final_fingerprint_change_publishes_no_authority(tmp_path):
    state, clock = __import__("test_phase7_harness").create_terminal_run(tmp_path)
    data = dataset(
        EvaluationCase(
            case_id="case_a",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="query",
        )
    )
    reader = ReadOnlyRunArtifactReader(tmp_path, max_input_bytes_per_case=1_000_000)

    class ChangingReader:
        def freeze(self, *args, **kwargs):
            return reader.freeze(*args, **kwargs)

        def fingerprint(self, *args, **kwargs):
            return stable_hash("changed")

    store = InMemoryEvaluationArtifactStore()
    harness = EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((data,)),
        artifacts=store,
        run_artifacts=ChangingReader(),
        clock=clock,
        evaluators=(DeterministicArtifactEvaluator(), ExactReferenceEvaluator()),
    )
    with pytest.raises(EvaluationInputChanged):
        asyncio.run(
            harness.evaluate_existing(
                request(data, state.run_id, clock), Cancellation()
            )
        )
    assert not store._runs


def test_ablation_identity_persistence_and_first_writer_cas(tmp_path):
    clock = Clock()
    manager = RunManager(
        store=FilesystemRunStore(tmp_path / "runs"),
        trace_sink=FilesystemTraceSink(tmp_path / "runs"),
        clock=clock,
        id_factory=Ids(),
    )
    left_state = _terminal(manager, "query", ("search",))
    right_state = _terminal(manager, "query", ("python", "search"))
    data = dataset(
        EvaluationCase(
            case_id="case_a",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="query",
        )
    )
    store, harness = _harness(tmp_path / "runs", data, clock)
    absent = ("planner_bundle", "runtime_schema", "verification_bundle")
    left_request = request(data, left_state.run_id, clock)
    left_request = left_request.model_copy(
        update={
            "bindings": (
                left_request.bindings[0].model_copy(
                    update={"absent_by_design_pin_ids": absent}
                ),
            )
        }
    )
    right_request = request(data, right_state.run_id, clock)
    right_request = right_request.model_copy(
        update={
            "bindings": (
                right_request.bindings[0].model_copy(
                    update={"absent_by_design_pin_ids": absent}
                ),
            )
        }
    )
    baseline = asyncio.run(
        harness.evaluate_existing(left_request, Cancellation())
    )
    candidate = asyncio.run(
        harness.evaluate_existing(right_request, Cancellation())
    )
    assert baseline.provenance_grade is ProvenanceGrade.PARTIALLY_VERIFIED
    definition = next(
        item
        for item in baseline.metric_definitions
        if item.metric_id == "execution_retry_count"
    )
    comparison = EvaluationComparisonService().compare(
        baseline, candidate, _regression_policy(definition)
    )
    spec = _spec(
        baseline,
        candidate,
        left_state.config_hash,
        right_state.config_hash,
    )
    result = AblationService().validate(
        spec, baseline, (candidate,), (comparison,)
    )
    assert result.causality is AblationCausality.VERIFIED_CAUSAL_COMPARISON
    changed_values = spec.model_dump()
    changed_values["one_dimension_only"] = False
    changed_hash = stable_hash(
        {
            **changed_values,
            "ablation_id": None,
            "ablation_spec_hash": None,
        }
    )
    assert changed_hash != spec.ablation_spec_hash
    changed_config_spec = _spec(
        baseline,
        candidate,
        left_state.config_hash,
        "f" * 64,
    )
    assert changed_config_spec.ablation_id != spec.ablation_id
    with pytest.raises(EvaluationProvenanceError, match="config hash"):
        AblationService().validate(
            changed_config_spec, baseline, (candidate,), (comparison,)
        )
    undeclared = _spec(
        baseline,
        candidate,
        left_state.config_hash,
        right_state.config_hash,
        changed_pin_ids=(),
        one_dimension_only=False,
    )
    with pytest.raises(EvaluationProvenanceError, match="undeclared"):
        AblationService().validate(
            undeclared, baseline, (candidate,), (comparison,)
        )

    fs = FilesystemEvaluationArtifactStore(tmp_path / "evaluations")
    changed_disposition = (
        ComparisonDisposition.FAIL
        if comparison.disposition is not ComparisonDisposition.FAIL
        else ComparisonDisposition.PASS
    )
    comparison_provisional = comparison.model_copy(
        update={
            "disposition": changed_disposition,
            "artifact_content_hash": "0" * 64,
        }
    )
    conflicting_comparison = comparison_provisional.model_copy(
        update={
            "artifact_content_hash": comparison_artifact_hash(
                comparison_provisional
            )
        }
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(fs.publish_comparison, item)
            for item in (comparison, conflicting_comparison)
        ]
        outcomes = []
        for future in futures:
            try:
                future.result()
                outcomes.append("ok")
            except EvaluationArtifactConflict:
                outcomes.append("conflict")
    assert sorted(outcomes) == ["conflict", "ok"]

    changed_causality = (
        AblationCausality.DECLARED_ASSOCIATION
        if result.causality is not AblationCausality.DECLARED_ASSOCIATION
        else AblationCausality.PARTIALLY_VERIFIED_COMPARISON
    )
    ablation_provisional = result.model_copy(
        update={
            "causality": changed_causality,
            "artifact_content_hash": "0" * 64,
        }
    )
    conflicting_ablation = ablation_provisional.model_copy(
        update={
            "artifact_content_hash": ablation_artifact_hash(ablation_provisional)
        }
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(fs.publish_ablation, item)
            for item in (result, conflicting_ablation)
        ]
        outcomes = []
        for future in futures:
            try:
                future.result()
                outcomes.append("ok")
            except EvaluationArtifactConflict:
                outcomes.append("conflict")
    assert sorted(outcomes) == ["conflict", "ok"]
    persisted_ablation = fs.load_ablation(result.ablation_id)
    assert persisted_ablation in (result, conflicting_ablation)
    assert persisted_ablation is not None
    fs.publish_ablation(persisted_ablation)
    path = tmp_path / "evaluations" / "ablations" / result.ablation_id / "ablation.json"
    path.write_bytes(b"{}\n")
    with pytest.raises(CorruptEvaluationArtifact):
        fs.load_ablation(result.ablation_id)


def test_verified_design_absence_can_be_a_verified_intervention(tmp_path):
    clock = Clock()
    manager = RunManager(
        store=FilesystemRunStore(tmp_path / "runs"),
        trace_sink=FilesystemTraceSink(tmp_path / "runs"),
        clock=clock,
        id_factory=Ids(),
    )
    baseline_state = _terminal(manager, "query")
    candidate_state = _terminal(manager, "query")
    example = runtime_example()
    assert example.state.run_id == baseline_state.run_id
    executor, _, _, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    FilesystemCheckpointStore(tmp_path / "runs").create(checkpoint)
    data = dataset(
        EvaluationCase(
            case_id="case_a",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="query",
        )
    )
    _, harness = _harness(tmp_path / "runs", data, clock)
    baseline_request = request(data, baseline_state.run_id, clock)
    baseline_request = baseline_request.model_copy(
        update={
            "bindings": (
                baseline_request.bindings[0].model_copy(
                    update={
                        "absent_by_design_pin_ids": ("verification_bundle",)
                    }
                ),
            )
        }
    )
    candidate_request = request(data, candidate_state.run_id, clock)
    candidate_request = candidate_request.model_copy(
        update={
            "bindings": (
                candidate_request.bindings[0].model_copy(
                    update={
                        "absent_by_design_pin_ids": (
                            "planner_bundle",
                            "runtime_schema",
                            "verification_bundle",
                        )
                    }
                ),
            )
        }
    )
    baseline = asyncio.run(
        harness.evaluate_existing(baseline_request, Cancellation())
    )
    candidate = asyncio.run(
        harness.evaluate_existing(candidate_request, Cancellation())
    )
    definition = next(
        item
        for item in baseline.metric_definitions
        if item.metric_id == "execution_retry_count"
    )
    comparison = EvaluationComparisonService().compare(
        baseline, candidate, _regression_policy(definition)
    )
    spec_values = dict(
        dataset_hash=baseline.dataset_hash,
        evaluation_policy_hash=baseline.evaluation_policy_hash,
        baseline=AblationArm(
            arm_id="baseline",
            eval_run_id=baseline.eval_run_id,
            declared_config_hash=baseline_state.config_hash,
            changed_pin_ids=(),
            absent_by_design_pin_ids=("verification_bundle",),
        ),
        candidates=(
            AblationArm(
                arm_id="candidate",
                eval_run_id=candidate.eval_run_id,
                declared_config_hash=candidate_state.config_hash,
                changed_pin_ids=(),
                absent_by_design_pin_ids=("planner_bundle", "runtime_schema"),
            ),
        ),
        one_dimension_only=False,
    )
    serialized = {
        **spec_values,
        "baseline": spec_values["baseline"].model_dump(mode="json"),
        "candidates": [
            item.model_dump(mode="json") for item in spec_values["candidates"]
        ],
    }
    spec_hash = stable_hash(serialized)
    spec = AblationSpec(
        **spec_values,
        ablation_id=stable_id("ablation", [spec_hash]),
        ablation_spec_hash=spec_hash,
    )
    result = AblationService().validate(
        spec, baseline, (candidate,), (comparison,)
    )
    assert result.causality is AblationCausality.VERIFIED_CAUSAL_COMPARISON


def test_in_memory_authorities_are_idempotent_conflicting_and_isolated(tmp_path):
    clock = Clock()
    manager = RunManager(
        store=FilesystemRunStore(tmp_path / "runs"),
        trace_sink=FilesystemTraceSink(tmp_path / "runs"),
        clock=clock,
        id_factory=Ids(),
    )
    baseline_state = _terminal(manager, "query", ("search",))
    candidate_state = _terminal(manager, "query", ("python", "search"))
    data = dataset(
        EvaluationCase(
            case_id="case_a",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="query",
        )
    )
    _, harness = _harness(tmp_path / "runs", data, clock)
    baseline = asyncio.run(
        harness.evaluate_existing(
            request(data, baseline_state.run_id, clock), Cancellation()
        )
    )
    candidate = asyncio.run(
        harness.evaluate_existing(
            request(data, candidate_state.run_id, clock), Cancellation()
        )
    )
    definition = next(
        item
        for item in baseline.metric_definitions
        if item.metric_id == "execution_retry_count"
    )
    comparison = EvaluationComparisonService().compare(
        baseline, candidate, _regression_policy(definition)
    )
    spec = _spec(
        baseline,
        candidate,
        baseline_state.config_hash,
        candidate_state.config_hash,
    )
    ablation = AblationService().validate(
        spec, baseline, (candidate,), (comparison,)
    )

    store = InMemoryEvaluationArtifactStore()
    store.publish(baseline)
    loaded_evaluation = store.load(baseline.eval_run_id)
    assert loaded_evaluation == baseline
    assert loaded_evaluation is not baseline
    assert loaded_evaluation.cases[0] is not baseline.cases[0]
    object.__setattr__(loaded_evaluation, "status", EvaluationRunStatus.FAILED)
    assert store.load(baseline.eval_run_id) == baseline

    store.publish_comparison(comparison)
    store.publish_comparison(comparison)
    loaded_comparison = store.load_comparison(comparison.comparison_id)
    assert loaded_comparison == comparison
    assert loaded_comparison is not comparison
    object.__setattr__(
        loaded_comparison,
        "disposition",
        ComparisonDisposition.FAIL
        if comparison.disposition is not ComparisonDisposition.FAIL
        else ComparisonDisposition.PASS,
    )
    assert store.load_comparison(comparison.comparison_id) == comparison
    comparison_provisional = comparison.model_copy(
        update={
            "disposition": (
                ComparisonDisposition.FAIL
                if comparison.disposition is not ComparisonDisposition.FAIL
                else ComparisonDisposition.PASS
            ),
            "artifact_content_hash": "0" * 64,
        }
    )
    conflicting_comparison = comparison_provisional.model_copy(
        update={
            "artifact_content_hash": comparison_artifact_hash(
                comparison_provisional
            )
        }
    )
    with pytest.raises(EvaluationArtifactConflict):
        store.publish_comparison(conflicting_comparison)

    store.publish_ablation(ablation)
    store.publish_ablation(ablation)
    loaded_ablation = store.load_ablation(ablation.ablation_id)
    assert loaded_ablation == ablation
    assert loaded_ablation is not ablation
    object.__setattr__(
        loaded_ablation,
        "causality",
        AblationCausality.DECLARED_ASSOCIATION
        if ablation.causality is not AblationCausality.DECLARED_ASSOCIATION
        else AblationCausality.PARTIALLY_VERIFIED_COMPARISON,
    )
    assert store.load_ablation(ablation.ablation_id) == ablation
    ablation_provisional = ablation.model_copy(
        update={
            "causality": (
                AblationCausality.DECLARED_ASSOCIATION
                if ablation.causality is not AblationCausality.DECLARED_ASSOCIATION
                else AblationCausality.PARTIALLY_VERIFIED_COMPARISON
            ),
            "artifact_content_hash": "0" * 64,
        }
    )
    conflicting_ablation = ablation_provisional.model_copy(
        update={
            "artifact_content_hash": ablation_artifact_hash(
                ablation_provisional
            )
        }
    )
    with pytest.raises(EvaluationArtifactConflict):
        store.publish_ablation(conflicting_ablation)


def test_concurrent_first_writer_cas_for_all_authorities(tmp_path):
    state, clock = __import__("test_phase7_harness").create_terminal_run(
        tmp_path / "runs"
    )
    data = dataset(
        EvaluationCase(
            case_id="case_a",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="query",
        )
    )
    _, harness = _harness(tmp_path / "runs", data, clock)
    result = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    other = _rehash_evaluation(
        result, completed_at=result.completed_at + timedelta(seconds=1)
    )
    store = FilesystemEvaluationArtifactStore(tmp_path / "cas")

    def publish(value):
        try:
            store.publish(value)
            return "ok"
        except EvaluationArtifactConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, (result, other)))
    assert sorted(outcomes) == ["conflict", "ok"]
    persisted = store.load(result.eval_run_id)
    assert persisted in (result, other)


def test_crash_before_authority_can_replay_with_new_occurrence_bytes(tmp_path):
    state, clock = __import__("test_phase7_harness").create_terminal_run(
        tmp_path / "runs"
    )
    data = dataset(
        EvaluationCase(
            case_id="case_a",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="query",
        )
    )
    _, harness = _harness(tmp_path / "runs", data, clock)
    result = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    changed_case = result.cases[0].model_copy(
        update={
            "completed_at": result.cases[0].completed_at + timedelta(seconds=1),
            "artifact_content_hash": "0" * 64,
        }
    )
    changed_case = changed_case.model_copy(
        update={"artifact_content_hash": case_artifact_hash(changed_case)}
    )
    replay = _rehash_evaluation(
        result,
        cases=(changed_case,),
        completed_at=result.completed_at + timedelta(seconds=1),
    )
    fail = True

    def fault(point):
        nonlocal fail
        if fail and point == "authority.before_temp_write":
            fail = False
            raise RuntimeError("injected")

    store = FilesystemEvaluationArtifactStore(tmp_path / "replay", fault=fault)
    with pytest.raises(EvaluationPersistenceError):
        store.publish(result)
    assert store.load(result.eval_run_id) is None
    store.publish(replay)
    assert store.load(replay.eval_run_id) == replay


def test_no_decrease_is_zero_and_target_rules_are_rejected(tmp_path):
    with pytest.raises(ValidationError, match="zero"):
        RegressionRule(
            metric_id="metric",
            definition_hash="0" * 64,
            threshold_kind=ThresholdKind.NO_DECREASE,
            allowed_degradation=0.1,
        )
    assert MetricDirection.TARGET.value == "target"
