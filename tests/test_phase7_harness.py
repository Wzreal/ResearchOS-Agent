from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from researchos.adapters.evaluation_dataset import InMemoryEvaluationDatasetLoader
from researchos.adapters.evaluation_memory import InMemoryEvaluationArtifactStore
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.application.errors import (
    CaseRunCompatibilityError,
    EvaluationCancelled,
    EvaluationDeadlineExceeded,
    EvaluationPreconditionError,
)
from researchos.application.evaluation_artifacts import ReadOnlyRunArtifactReader
from researchos.application.evaluation_evaluators import (
    DeterministicArtifactEvaluator,
    ExactReferenceEvaluator,
)
from researchos.application.evaluation_harness import EvaluationHarness
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import RunConfig, RunInput, RunStatus
from researchos.domain.evaluation import (
    CaseRunBinding,
    DatasetProvenance,
    EvaluationCase,
    EvaluationDataset,
    EvaluationPolicy,
    EvaluationRequest,
    ReferenceAnnotations,
    ReferenceLevel,
    RequiredExecutionConditions,
    SourcePolicyRequirement,
    SourcePolicyRequirementKind,
)
from researchos.domain.identity import stable_hash


class Clock:
    def __init__(self):
        self.current = datetime(2026, 8, 30, tzinfo=UTC)

    def now(self):
        return self.current


class Ids:
    def __init__(self):
        self.value = 0

    def __call__(self, prefix):
        self.value += 1
        return f"{prefix}_{self.value}"


class Cancellation:
    def __init__(self, cancelled=False):
        self._cancelled = cancelled

    @property
    def cancelled(self):
        return self._cancelled

    async def wait(self):
        if self._cancelled:
            return
        await asyncio.Future()


def dataset(case: EvaluationCase) -> EvaluationDataset:
    values = dict(
        dataset_id="dataset_a",
        dataset_version="1",
        provenance=DatasetProvenance(
            license_id="test_license",
            source_uri_hash="0" * 64,
            curator_id="tests",
            provenance_version="1",
        ),
        cases=(case,),
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


def create_terminal_run(root, *, query="query", source_policy=None):
    clock = Clock()
    manager = RunManager(
        store=FilesystemRunStore(root),
        trace_sink=FilesystemTraceSink(root),
        clock=clock,
        id_factory=Ids(),
    )
    state = manager.create(
        RunInput(query=query),
        RunConfig(allowed_capability_ids=("search",), source_policy_id=source_policy),
    )
    for status in (
        RunStatus.PLANNING,
        RunStatus.READY,
        RunStatus.RUNNING,
        RunStatus.VERIFYING,
        RunStatus.EVALUATING,
    ):
        state = manager.transition(state.run_id, status)
    state = manager.finalize(state.run_id, RunStatus.COMPLETED)
    return state, clock


def build_harness(root, case, clock):
    data = dataset(case)
    store = InMemoryEvaluationArtifactStore()
    harness = EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((data,)),
        artifacts=store,
        run_artifacts=ReadOnlyRunArtifactReader(
            root, max_input_bytes_per_case=1_000_000
        ),
        clock=clock,
        evaluators=(DeterministicArtifactEvaluator(), ExactReferenceEvaluator()),
    )
    return data, store, harness


def request(data, run_id, clock):
    return EvaluationRequest(
        dataset_id=data.dataset_id,
        dataset_version=data.dataset_version,
        dataset_hash=data.dataset_content_hash,
        bindings=(CaseRunBinding(case_id=data.cases[0].case_id, run_id=run_id),),
        commit_sha="a" * 40,
        system_version="1",
        policy=EvaluationPolicy(
            policy_id="evaluation_policy",
            policy_version="1",
            enabled_evaluator_ids=("deterministic_core", "reference_exact"),
        ),
        deadline=clock.now() + timedelta(minutes=1),
    )


def test_wrong_case_run_binding_fails_before_evaluator(tmp_path):
    state, clock = create_terminal_run(tmp_path, query="actual")
    case = EvaluationCase(
        case_id="case_a", reference_level=ReferenceLevel.STRUCTURAL_ONLY, query="other"
    )
    data, store, harness = build_harness(tmp_path, case, clock)
    with pytest.raises(CaseRunCompatibilityError):
        asyncio.run(
            harness.evaluate_existing(
                request(data, state.run_id, clock), Cancellation()
            )
        )
    assert store.load("anything") is None


def test_reference_capability_does_not_trigger_preflight_and_replay_is_zero_call(
    tmp_path,
):
    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.PARTIAL_GOLD,
        query="query",
        reference_annotations=ReferenceAnnotations(expected_capability_ids=("python",)),
    )
    data, _, harness = build_harness(tmp_path, case, clock)
    first = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    second = asyncio.run(
        harness.evaluate_existing(request(data, state.run_id, clock), Cancellation())
    )
    assert second == first
    metric = next(
        item
        for item in first.cases[0].metrics
        if item.metric_id == "planning_expected_capability_coverage"
    )
    assert (
        metric.status.value == "unavailable"
    )  # no checkpoint, never preflight rejection


def test_source_policy_require_none_and_exact_are_enforced(tmp_path):
    state, clock = create_terminal_run(tmp_path, source_policy="policy_a")
    for requirement in (
        SourcePolicyRequirement(kind=SourcePolicyRequirementKind.REQUIRE_NONE),
        SourcePolicyRequirement(
            kind=SourcePolicyRequirementKind.REQUIRE_EXACT, policy_id="policy_b"
        ),
    ):
        case = EvaluationCase(
            case_id="case_a",
            reference_level=ReferenceLevel.STRUCTURAL_ONLY,
            query="query",
            required_execution_conditions=RequiredExecutionConditions(
                source_policy=requirement
            ),
        )
        data, _, harness = build_harness(tmp_path, case, clock)
        with pytest.raises(CaseRunCompatibilityError):
            asyncio.run(
                harness.evaluate_existing(
                    request(data, state.run_id, clock), Cancellation()
                )
            )


def test_cancellation_publishes_no_authority(tmp_path):
    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a", reference_level=ReferenceLevel.STRUCTURAL_ONLY, query="query"
    )
    data, store, harness = build_harness(tmp_path, case, clock)
    with pytest.raises(EvaluationCancelled):
        asyncio.run(
            harness.evaluate_existing(
                request(data, state.run_id, clock), Cancellation(cancelled=True)
            )
        )
    assert not store._runs


def test_deadline_and_input_bound_publish_no_authority(tmp_path):
    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data, store, harness = build_harness(tmp_path, case, clock)
    expired = request(data, state.run_id, clock).model_copy(
        update={"deadline": clock.now()}
    )
    with pytest.raises(EvaluationDeadlineExceeded):
        asyncio.run(harness.evaluate_existing(expired, Cancellation()))
    assert not store._runs

    bounded = request(data, state.run_id, clock)
    bounded = bounded.model_copy(
        update={
            "policy": bounded.policy.model_copy(update={"max_input_bytes_per_case": 1})
        }
    )
    with pytest.raises(EvaluationPreconditionError, match="input artifact byte"):
        asyncio.run(harness.evaluate_existing(bounded, Cancellation()))
    assert not store._runs


def test_absent_by_design_cannot_hide_present_pin(tmp_path):
    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data, store, harness = build_harness(tmp_path, case, clock)
    value = request(data, state.run_id, clock)
    value = value.model_copy(
        update={
            "bindings": (
                value.bindings[0].model_copy(
                    update={"absent_by_design_pin_ids": ("operating_mode",)}
                ),
            )
        }
    )
    with pytest.raises(EvaluationPreconditionError, match="artifact is present"):
        asyncio.run(harness.evaluate_existing(value, Cancellation()))
    assert not store._runs


def test_evaluator_failure_is_partial_not_fabricated_success(tmp_path):
    class FailingEvaluator:
        evaluator_id = "deterministic_core"
        evaluator_version = "1"
        definitions = DeterministicArtifactEvaluator.definitions

        def evaluate(self, case, frozen):
            del case, frozen
            raise RuntimeError("injected")

    state, clock = create_terminal_run(tmp_path)
    case = EvaluationCase(
        case_id="case_a",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )
    data = dataset(case)
    store = InMemoryEvaluationArtifactStore()
    harness = EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((data,)),
        artifacts=store,
        run_artifacts=ReadOnlyRunArtifactReader(
            tmp_path, max_input_bytes_per_case=1_000_000
        ),
        clock=clock,
        evaluators=(FailingEvaluator(),),
    )
    value = request(data, state.run_id, clock).model_copy(
        update={
            "policy": EvaluationPolicy(
                policy_id="evaluation_policy",
                policy_version="1",
                enabled_evaluator_ids=("deterministic_core",),
            )
        }
    )
    result = asyncio.run(harness.evaluate_existing(value, Cancellation()))
    assert result.status.value == "partial"
    assert result.cases[0].failures[0].code == "evaluator_failed"
    assert all(metric.status.value == "error" for metric in result.cases[0].metrics)
