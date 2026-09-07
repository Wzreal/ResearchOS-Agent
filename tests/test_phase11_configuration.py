from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_phase7_hardening import (
    Clock,
    Ids,
    _harness,
    _regression_policy,
    _spec,
    _terminal,
)

from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.application.evaluation_artifacts import ReadOnlyRunArtifactReader
from researchos.application.evaluation_comparison import (
    AblationService,
    EvaluationComparisonService,
)
from researchos.application.evaluation_evaluators import (
    DeterministicArtifactEvaluator,
    ExactReferenceEvaluator,
)
from researchos.application.phase11_evaluation import (
    Phase11OperationalEvaluator,
    project_phase11_metrics,
)
from researchos.application.run_manager import RunManager
from researchos.configuration.phase10_mock import build_phase10_mock_bundle_v1
from researchos.configuration.phase11 import (
    PHASE11_HARD_COST_CEILING_MICROUNITS,
    PHASE11_SECONDARY_REVIEW_CASE_IDS_V1,
    Phase11BatchCapacity,
    Phase11PaidAdmission,
    Phase11RealBenchmarkBundleV1,
    phase11_evaluation_policy,
    phase11_secondary_review_subset_hash,
    require_paid_admission,
)
from researchos.configuration.phase11_benchmark import (
    build_phase11_ablation_subset_v1,
    build_phase11_real_benchmark_v1,
)
from researchos.domain.contracts import (
    OperatingMode,
    RunConfig,
    RunInput,
    RunStatus,
    model_sha256,
)
from researchos.domain.evaluation import CaseRunBinding
from researchos.domain.phase11 import (
    Phase11HumanReviewV1,
    Phase11ReviewRubricV1,
)
from researchos.domain.runtime import UsageCertainty


def test_benchmark_is_exactly_twelve_cases_and_deterministic() -> None:
    first = build_phase11_real_benchmark_v1()
    second = build_phase11_real_benchmark_v1()
    assert len(first.cases) == 12
    assert first == second
    assert first.dataset_content_hash == second.dataset_content_hash
    assert {
        "factual",
        "comparison",
        "time_bounded",
        "evidence_synthesis",
        "conflict_handling",
        "citation_heavy",
        "multi_step",
    }.issubset({tag for case in first.cases for tag in case.tags})
    subset = build_phase11_ablation_subset_v1(comparison_id="max_rounds_1_vs_2")
    assert len(subset.cases) == 4
    assert (
        subset.dataset_content_hash
        == build_phase11_ablation_subset_v1(
            comparison_id="max_rounds_1_vs_2"
        ).dataset_content_hash
    )
    assert all(
        "Reference claim for" not in claim.statement
        for case in first.cases
        for claim in case.reference_annotations.claims
    )
    assert all("reference_locator" in case.metadata for case in first.cases)


def test_multisource_benchmark_cases_pin_distinct_reference_locators() -> None:
    benchmark = build_phase11_real_benchmark_v1()
    for case in benchmark.cases:
        if {"multi_source", "comparison", "conflict_handling"}.intersection(case.tags):
            hashes = {
                item.canonical_locator_hash
                for item in case.reference_annotations.evidence
            }
            assert len(hashes) >= 2, case.case_id
            assert len(case.reference_annotations.citations) >= 2, case.case_id
            constraint_ids = {
                item.constraint_id for item in case.reference_annotations.evidence
            }
            assert {
                item.evidence_constraint_id
                for item in case.reference_annotations.citations
            } == constraint_ids


def test_phase11_operational_evaluator_is_declared_and_preserves_unavailable_values(
    tmp_path,
) -> None:
    profile = build_phase10_mock_bundle_v1().workflow_profile
    assert (
        "phase11_operational"
        in phase11_evaluation_policy(profile).enabled_evaluator_ids
    )
    clock = Clock()
    manager = RunManager(
        store=FilesystemRunStore(tmp_path),
        trace_sink=FilesystemTraceSink(tmp_path),
        clock=clock,
        id_factory=Ids(),
    )
    state = _terminal(manager, "operational evaluator", ("web_search",))
    frozen = ReadOnlyRunArtifactReader(
        tmp_path, max_input_bytes_per_case=1_000_000
    ).freeze(state.run_id)
    evaluator = Phase11OperationalEvaluator()
    known = evaluator.evaluate(None, frozen)
    unavailable = evaluator.evaluate(None, replace(frozen, run_state=None))
    assert known[1].value_type == "integer"
    assert all(item.status == "unavailable" for item in unavailable)
    projection = project_phase11_metrics(frozen)
    assert projection.cost_microunits is None
    assert projection.cost_certainty is UsageCertainty.UNKNOWN


def test_bundle_is_configuration_only_and_pins_profile() -> None:
    profile = build_phase10_mock_bundle_v1().workflow_profile
    bundle = Phase11RealBenchmarkBundleV1(
        bundle_id="phase11_real_benchmark",
        bundle_version="1",
        workflow_profile=profile,
        benchmark=build_phase11_real_benchmark_v1(),
        evaluation_policy=phase11_evaluation_policy(profile),
        real_settings_hash="a" * 64,
        capability_ids=("web_search",),
        approved_refs=("main",),
        max_agent_steps=2,
        max_agent_tool_calls=1,
        system_commit_sha=profile.system_commit_sha,
        system_version=profile.system_version,
        max_batch_cost_microunits=PHASE11_HARD_COST_CEILING_MICROUNITS,
    )
    assert bundle.bundle_hash == model_sha256(bundle)


def test_paid_admission_fails_closed_before_dispatch() -> None:
    admission = Phase11PaidAdmission(
        expected_commit_sha="a" * 40,
        approved_cost_microunits=10,
        real_acknowledged=True,
    )
    assert (
        require_paid_admission(
            admission,
            current_commit_sha="a" * 40,
            is_clean=True,
            approved_ref=True,
            next_reservation_microunits=10,
        )
        == 10
    )
    with pytest.raises(ValueError, match="approved capacity"):
        require_paid_admission(
            admission,
            current_commit_sha="a" * 40,
            is_clean=True,
            approved_ref=True,
            next_reservation_microunits=11,
        )


def test_batch_capacity_cannot_reuse_one_approval() -> None:
    capacity = Phase11BatchCapacity(10)
    capacity.reserve(6)
    assert capacity.remaining_microunits == 4
    with pytest.raises(ValueError, match="batch capacity"):
        capacity.reserve(6)


def test_secondary_human_review_requires_blinding_and_provenance() -> None:
    rubric = Phase11ReviewRubricV1(
        rubric_id="quality_rubric",
        rubric_version="1",
        dimensions=("citation", "correctness"),
    )
    review = Phase11HumanReviewV1(
        run_id="run_1",
        evaluation_run_id="eval_1",
        reviewer_id="reviewer_2",
        rubric=rubric,
        evidence_reference="report.md#claim-1",
        output_hash="a" * 64,
        scores={"citation": 1, "correctness": 2},
        rationale="Reference evidence and output were reviewed.",
        reviewed_at=datetime(2026, 9, 5, tzinfo=UTC),
        blinded=True,
        review_kind="secondary",
    )
    assert review.blinded
    assert len(PHASE11_SECONDARY_REVIEW_CASE_IDS_V1) == 6
    assert len(set(PHASE11_SECONDARY_REVIEW_CASE_IDS_V1)) == 6
    assert (
        phase11_secondary_review_subset_hash() == phase11_secondary_review_subset_hash()
    )
    by_case = {item.case_id: item for item in build_phase11_real_benchmark_v1().cases}
    selected_tags = {
        tag
        for case_id in PHASE11_SECONDARY_REVIEW_CASE_IDS_V1
        for tag in by_case[case_id].tags
    }
    assert {
        "citation_heavy",
        "comparison",
        "conflict_handling",
        "evidence_synthesis",
        "multi_source",
        "time_bounded",
    }.issubset(selected_tags)
    with pytest.raises(ValueError, match="blinded"):
        Phase11HumanReviewV1(
            **review.model_dump(mode="python", exclude={"blinded"}), blinded=False
        )


def _phase11_ablation_runs(
    tmp_path,
    *,
    comparison_id,
    baseline_capabilities,
    candidate_capabilities,
):
    clock = Clock()

    class OfflineRealGuard:
        def validate_create(self, config):
            del config

        def bind_created(self, state):
            del state

        def validate_resume(self, state):
            del state

        def validate_bound_state(self, state):
            del state

        def validate_transition(self, state, target):
            del state, target

    manager = RunManager(
        store=FilesystemRunStore(tmp_path),
        trace_sink=FilesystemTraceSink(tmp_path),
        clock=clock,
        id_factory=Ids(),
        integration_guard=OfflineRealGuard(),
    )
    dataset = build_phase11_ablation_subset_v1(comparison_id=comparison_id)

    def terminal(query, capabilities):
        state = manager.create(
            RunInput(query=query),
            RunConfig(
                mode=OperatingMode.REAL,
                allowed_capability_ids=capabilities,
            ),
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

    baseline_states = tuple(
        terminal(item.query, baseline_capabilities) for item in dataset.cases
    )
    candidate_states = tuple(
        terminal(item.query, candidate_capabilities) for item in dataset.cases
    )
    _, harness = _harness(
        tmp_path,
        dataset,
        clock,
        evaluators=(
            DeterministicArtifactEvaluator(),
            ExactReferenceEvaluator(),
            Phase11OperationalEvaluator(),
        ),
    )
    policy = phase11_evaluation_policy(build_phase10_mock_bundle_v1().workflow_profile)
    baseline = __import__("asyncio").run(
        harness.evaluate_existing(
            __import__("test_phase7_hardening")._request(
                dataset,
                tuple(
                    CaseRunBinding(case_id=item.case_id, run_id=state.run_id)
                    for item, state in zip(dataset.cases, baseline_states, strict=True)
                ),
                clock,
                policy=policy,
            ),
            __import__("test_phase7_harness").Cancellation(),
        )
    )
    candidate = __import__("asyncio").run(
        harness.evaluate_existing(
            __import__("test_phase7_hardening")._request(
                dataset,
                tuple(
                    CaseRunBinding(case_id=item.case_id, run_id=state.run_id)
                    for item, state in zip(dataset.cases, candidate_states, strict=True)
                ),
                clock,
                policy=policy,
            ),
            __import__("test_phase7_harness").Cancellation(),
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
    result = AblationService().validate(
        _spec(
            baseline,
            candidate,
            baseline_states[0].config_hash,
            candidate_states[0].config_hash,
        ),
        baseline,
        (candidate,),
        (comparison,),
    )
    return dataset, baseline, candidate, comparison, result


@pytest.mark.parametrize(
    ("comparison_id", "baseline_capabilities", "candidate_capabilities"),
    (
        ("max_rounds_1_vs_2", ("web_search", "web_browser"), ("web_search",)),
        (
            "search_browser_vs_search_only",
            ("web_search", "web_browser"),
            ("web_search",),
        ),
    ),
)
def test_phase11_ablation_uses_phase7_comparison_and_ablation_services(
    tmp_path, comparison_id, baseline_capabilities, candidate_capabilities
) -> None:
    dataset, baseline, candidate, comparison, result = _phase11_ablation_runs(
        tmp_path,
        comparison_id=comparison_id,
        baseline_capabilities=baseline_capabilities,
        candidate_capabilities=candidate_capabilities,
    )
    assert (
        baseline.dataset_hash == candidate.dataset_hash == dataset.dataset_content_hash
    )
    assert (
        baseline.case_ids
        == candidate.case_ids
        == tuple(item.case_id for item in dataset.cases)
    )
    assert baseline.evaluation_policy_hash == candidate.evaluation_policy_hash
    assert baseline.evaluator_bundle_hash == candidate.evaluator_bundle_hash
    assert baseline.system.commit_sha == candidate.system.commit_sha
    assert baseline.system.system_version == candidate.system.system_version
    assert comparison.baseline_eval_run_id == baseline.eval_run_id
    assert result.ablation_spec.baseline.eval_run_id == baseline.eval_run_id
