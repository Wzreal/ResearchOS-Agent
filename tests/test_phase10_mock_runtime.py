from __future__ import annotations

from datetime import UTC, datetime

from conftest import FrozenClock

from researchos.adapters.mock_agent import ScriptedAgent
from researchos.adapters.mock_planning import MockPlanningModel
from researchos.application.workflow_coordinator import WorkflowCoordinator
from researchos.application.workflow_factory import WorkflowFactory
from researchos.configuration.phase10_mock import build_phase10_mock_bundle_v1
from researchos.configuration.phase10_mock_runtime import (
    DeterministicMockSearchTool,
    Phase10MockClaimExtractionModel,
    Phase10MockVerificationModel,
    build_phase10_mock_runtime,
)
from researchos.domain.planning import PlanningRequest, RemainingBudget


def test_runtime_builder_constructs_all_mock_components_for_arbitrary_query() -> None:
    bundle = build_phase10_mock_bundle_v1()
    first = build_phase10_mock_runtime(bundle, "a fresh arbitrary question")
    second = build_phase10_mock_runtime(bundle, "a fresh arbitrary question")

    assert isinstance(first.planning_model, MockPlanningModel)
    assert isinstance(first.agent, ScriptedAgent)
    assert isinstance(first.tools[0], DeterministicMockSearchTool)
    assert isinstance(first.claim_extraction_model, Phase10MockClaimExtractionModel)
    assert isinstance(first.verification_model, Phase10MockVerificationModel)
    assert first.semantic_identity == second.semantic_identity


def test_runtime_planning_fixture_is_exact_for_current_query() -> None:
    runtime = build_phase10_mock_runtime(
        build_phase10_mock_bundle_v1(), "a fresh arbitrary question"
    )
    request = PlanningRequest(
        request_id="request_1",
        run_id="run_1",
        run_revision=1,
        plan_id="plan_1",
        query="a fresh arbitrary question",
        output_format="markdown",
        requested_at=datetime(2026, 1, 1, tzinfo=UTC),
        allowed_capability_ids=("read", "search"),
        remaining_budget=RemainingBudget(
            duration_milliseconds=6_000,
            tokens=600,
            cost_microunits=600,
            tool_calls=6,
        ),
        policy=build_phase10_mock_bundle_v1().workflow_profile.planning_policy,
    )

    assert runtime.planning_model.generate(request).payload["run_id"] == "run_1"


def test_factory_build_mock_requires_no_live_adapter_arguments(tmp_path) -> None:
    coordinator = WorkflowFactory(tmp_path).build_mock(
        bundle=build_phase10_mock_bundle_v1(),
        query="factory construction question",
        clock=FrozenClock(),
    )

    assert isinstance(coordinator, WorkflowCoordinator)
