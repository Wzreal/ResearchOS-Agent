from __future__ import annotations

import pytest
from conftest import FrozenClock, SequentialIds
from planning_fixtures import candidate_payload, model_response, planning_policy

from researchos.adapters.memory import InMemoryRunStore, InMemoryTraceSink
from researchos.adapters.mock_planning import MockPlanningModel, PlanningFixtureKey
from researchos.application.dag_validator import DAGValidator
from researchos.application.errors import (
    PlanningModelFailure,
    PlanningPreconditionError,
)
from researchos.application.perspective_planner import PerspectivePlanner
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import RunConfig, RunInput, RunStatus, TraceEventType
from researchos.domain.planning import (
    PlanningModelResponse,
    PlanningStatus,
    ReplanContext,
    ReplanDecisionStatus,
    ReplanPolicy,
    ReplanRequest,
    ValidationIssueCode,
)


def planning_state(*, query: str = "normalized query"):
    store = InMemoryRunStore()
    trace = InMemoryTraceSink()
    clock = FrozenClock()
    ids = SequentialIds()
    manager = RunManager(
        store=store, trace_sink=trace, clock=clock, id_factory=ids
    )
    created = manager.create(
        RunInput(query=query),
        RunConfig(
            allowed_capability_ids=("search", "read"),
            source_policy_id="sources_primary",
            output_format="json",
        ),
    )
    return manager.transition(created.run_id, RunStatus.PLANNING), trace, clock, ids


def planner_for(model, trace, clock, ids) -> PerspectivePlanner:
    return PerspectivePlanner(
        model=model,
        validator=DAGValidator(),
        trace_sink=trace,
        clock=clock,
        id_factory=ids,
    )


def test_mock_planner_returns_validated_dag_and_ordered_trace() -> None:
    state, trace, clock, ids = planning_state(query="  normalized query  ")
    model = MockPlanningModel(
        {
            PlanningFixtureKey("normalized query", 0, None): model_response,
        }
    )
    planner = planner_for(model, trace, clock, ids)

    result = planner.plan(state, planning_policy())

    assert result.status is PlanningStatus.VALIDATED
    assert result.validation is not None and result.validation.valid
    assert result.validated_dag is not None
    assert result.run_revision == state.revision
    assert result.validated_dag.run_revision == state.revision
    planning_request = model.requests[0]
    assert planning_request.run_revision == state.revision
    assert planning_request.source_policy_id == "sources_primary"
    assert planning_request.output_format == "json"
    assert planning_request.requested_at == clock.now()
    planning_events = [
        event.event_type
        for event in trace.read(state.run_id)
        if event.event_type.value.startswith("planning.")
    ]
    assert planning_events == [
        TraceEventType.PLANNING_STARTED,
        TraceEventType.PLANNING_CANDIDATE_RECEIVED,
        TraceEventType.PLANNING_VALIDATED,
    ]
    assert all(
        event.revision == state.revision
        for event in trace.read(state.run_id)
        if event.event_type.value.startswith("planning.")
    )


def test_malformed_candidate_has_real_validation_but_model_error_does_not() -> None:
    state, trace, clock, ids = planning_state()
    malformed_model = MockPlanningModel(
        {
            PlanningFixtureKey("normalized query", 0, None): PlanningModelResponse(
                planning_model_id="mock_model", payload={"not": "a candidate"}
            )
        }
    )
    malformed = planner_for(malformed_model, trace, clock, ids).plan(
        state, planning_policy()
    )
    assert malformed.status is PlanningStatus.MALFORMED
    assert malformed.validation is not None
    assert (
        malformed.validation.issues[0].code
        is ValidationIssueCode.CANDIDATE_MALFORMED
    )

    other_state, other_trace, other_clock, other_ids = planning_state()
    failed_model = MockPlanningModel(
        {
            PlanningFixtureKey("normalized query", 0, None): PlanningModelFailure(
                "provider_unavailable", "provider failed", retryable=True
            )
        }
    )
    failed = planner_for(failed_model, other_trace, other_clock, other_ids).plan(
        other_state, planning_policy()
    )
    assert failed.status is PlanningStatus.MODEL_ERROR
    assert failed.validation is None
    assert failed.planning_error is not None


def test_malformed_candidate_error_details_do_not_leak_sensitive_input() -> None:
    state, trace, clock, ids = planning_state()
    secret = "highly-sensitive-free-text-928374"

    def malformed_with_secret(request):
        payload = candidate_payload(request)
        payload["tasks"][0]["objective"] = {"private": secret}
        payload[secret] = "unexpected field"
        return PlanningModelResponse(planning_model_id="mock_model", payload=payload)

    model = MockPlanningModel(
        {
            PlanningFixtureKey(
                "normalized query", 0, None
            ): malformed_with_secret
        }
    )
    result = planner_for(model, trace, clock, ids).plan(state, planning_policy())
    persisted_view = result.model_dump_json() + "".join(
        event.model_dump_json() for event in trace.read(state.run_id)
    )
    assert result.status is PlanningStatus.MALFORMED
    assert secret not in persisted_view
    assert result.validation is not None
    errors = result.validation.issues[0].details["errors"]
    assert errors
    assert all(set(error) == {"loc", "type", "message"} for error in errors)
    assert all("input" not in error and "url" not in error for error in errors)


def test_provenance_mismatch_is_invalid() -> None:
    state, trace, clock, ids = planning_state()

    def mismatch(request):
        return PlanningModelResponse(
            planning_model_id="other_model", payload=candidate_payload(request)
        )

    model = MockPlanningModel(
        {PlanningFixtureKey("normalized query", 0, None): mismatch}
    )
    result = planner_for(model, trace, clock, ids).plan(state, planning_policy())
    assert result.status is PlanningStatus.INVALID
    assert result.validation is not None
    assert ValidationIssueCode.PLANNER_PROVENANCE_MISMATCH in {
        issue.code for issue in result.validation.issues
    }


@pytest.mark.parametrize(
    ("scenario", "expected_code", "policy"),
    [
        (
            "unknown_dependency",
            ValidationIssueCode.UNKNOWN_DEPENDENCY,
            planning_policy(),
        ),
        ("cycle", ValidationIssueCode.CYCLE_DETECTED, planning_policy()),
        (
            "unauthorized_capability",
            ValidationIssueCode.UNAUTHORIZED_CAPABILITY,
            planning_policy(),
        ),
        ("budget", ValidationIssueCode.BUDGET_TOKENS_EXCEEDED, planning_policy()),
        (
            "over_task_limit",
            ValidationIssueCode.TASK_LIMIT_EXCEEDED,
            planning_policy(max_tasks=2),
        ),
    ],
)
def test_mock_supports_required_invalid_scenarios(
    scenario, expected_code, policy
) -> None:
    state, trace, clock, ids = planning_state()

    def fixture(request):
        payload = candidate_payload(request)
        if scenario == "unknown_dependency":
            payload["tasks"][0]["dependencies"] = [{"task_id": "missing"}]
        elif scenario == "cycle":
            payload["tasks"][0]["dependencies"] = [{"task_id": "task_c"}]
        elif scenario == "unauthorized_capability":
            payload["tasks"][0]["required_capability_ids"] = ["browser"]
        elif scenario == "budget":
            payload["tasks"][0]["estimate"]["tokens"] = 100_001
        return PlanningModelResponse(planning_model_id="mock_model", payload=payload)

    model = MockPlanningModel(
        {PlanningFixtureKey("normalized query", 0, None): fixture}
    )
    result = planner_for(model, trace, clock, ids).plan(state, policy)
    assert result.status is PlanningStatus.INVALID
    assert result.validation is not None
    assert expected_code in {issue.code for issue in result.validation.issues}


def test_fixture_key_drives_initial_invalid_then_replan_valid() -> None:
    state, trace, clock, ids = planning_state()

    def invalid_candidate(request):
        payload = candidate_payload(request)
        payload["tasks"][0]["dependencies"] = [{"task_id": "missing"}]
        return PlanningModelResponse(planning_model_id="mock_model", payload=payload)

    model = MockPlanningModel(
        {
            PlanningFixtureKey("normalized query", 0, None): invalid_candidate,
            PlanningFixtureKey(
                "normalized query", 1, "repair_invalid_dag"
            ): model_response,
        }
    )
    planner = planner_for(model, trace, clock, ids)
    initial = planner.plan(state, planning_policy())
    request = ReplanRequest(
        request_id="replan_request",
        run_id=state.run_id,
        context=initial.replan_context,
        reason_code="repair_invalid_dag",
        reason="remove the unknown dependency",
    )

    replanned = planner.replan(
        state,
        prior_result=initial,
        request=request,
        planning_policy=planning_policy(),
        replan_policy=ReplanPolicy(max_replans=1),
    )

    assert initial.status is PlanningStatus.INVALID
    assert replanned.decision.status is ReplanDecisionStatus.APPROVED
    assert replanned.planning_result is not None
    assert replanned.planning_result.status is PlanningStatus.VALIDATED
    assert replanned.planning_result.replan_context.replan_count == 1
    assert (
        replanned.planning_result.replan_context.prior_decision_id
        == replanned.decision.decision_id
    )
    assert [request.replan_count for request in model.requests] == [0, 1]


def test_replan_rejects_reset_or_inconsistent_context_without_model_call() -> None:
    state, trace, clock, ids = planning_state()
    model = MockPlanningModel(
        {PlanningFixtureKey("normalized query", 0, None): model_response}
    )
    planner = planner_for(model, trace, clock, ids)
    initial = planner.plan(state, planning_policy())
    forged = ReplanContext(
        prior_plan_id=initial.plan_id,
        replan_count=0,
        prior_decision_id="decision_forged",
    )
    result = planner.replan(
        state,
        prior_result=initial,
        request=ReplanRequest(
            request_id="replan_forged",
            run_id=state.run_id,
            context=forged,
            reason_code="retry",
            reason="try again",
        ),
        planning_policy=planning_policy(),
        replan_policy=ReplanPolicy(max_replans=5),
    )
    assert result.decision.status is ReplanDecisionStatus.REJECTED_CONTEXT
    assert result.planning_result is None
    assert len(model.requests) == 1


def test_approved_replan_consumes_prior_context() -> None:
    state, trace, clock, ids = planning_state()
    model = MockPlanningModel(
        {
            PlanningFixtureKey("normalized query", 0, None): model_response,
            PlanningFixtureKey("normalized query", 1, "retry"): model_response,
        }
    )
    planner = planner_for(model, trace, clock, ids)
    initial = planner.plan(state, planning_policy())
    request = ReplanRequest(
        request_id="replan_first",
        run_id=state.run_id,
        context=initial.replan_context,
        reason_code="retry",
        reason="try once",
    )
    first = planner.replan(
        state,
        prior_result=initial,
        request=request,
        planning_policy=planning_policy(),
        replan_policy=ReplanPolicy(max_replans=2),
    )
    repeated = planner.replan(
        state,
        prior_result=initial,
        request=request.model_copy(update={"request_id": "replan_repeated"}),
        planning_policy=planning_policy(),
        replan_policy=ReplanPolicy(max_replans=2),
    )
    assert first.decision.status is ReplanDecisionStatus.APPROVED
    assert repeated.decision.status is ReplanDecisionStatus.REJECTED_CONTEXT
    assert len(model.requests) == 2


def test_replan_limit_rejection_and_trace_order() -> None:
    state, trace, clock, ids = planning_state()
    model = MockPlanningModel(
        {PlanningFixtureKey("normalized query", 0, None): model_response}
    )
    planner = planner_for(model, trace, clock, ids)
    initial = planner.plan(state, planning_policy())
    result = planner.replan(
        state,
        prior_result=initial,
        request=ReplanRequest(
            request_id="replan_limited",
            run_id=state.run_id,
            context=initial.replan_context,
            reason_code="retry",
            reason="try again",
        ),
        planning_policy=planning_policy(),
        replan_policy=ReplanPolicy(max_replans=0),
    )
    assert result.decision.status is ReplanDecisionStatus.REJECTED_LIMIT
    assert len(model.requests) == 1
    assert [event.event_type for event in trace.read(state.run_id)][-2:] == [
        TraceEventType.REPLAN_REQUESTED,
        TraceEventType.REPLAN_DECISION,
    ]


def test_planning_requires_planning_lifecycle_state() -> None:
    state, trace, clock, ids = planning_state()
    model = MockPlanningModel(
        {PlanningFixtureKey("normalized query", 0, None): model_response}
    )
    with pytest.raises(PlanningPreconditionError):
        planner_for(model, trace, clock, ids).plan(
            state.model_copy(update={"status": RunStatus.READY}), planning_policy()
        )


def test_mock_has_no_implicit_default_fixture() -> None:
    state, trace, clock, ids = planning_state()
    result = planner_for(MockPlanningModel({}), trace, clock, ids).plan(
        state, planning_policy()
    )
    assert result.status is PlanningStatus.MODEL_ERROR
    assert result.planning_error is not None
    assert result.planning_error.code == "mock_fixture_not_found"


def test_fresh_planner_can_consume_checkpoint_trusted_lineage() -> None:
    state, trace, clock, ids = planning_state()
    model = MockPlanningModel(
        {
            PlanningFixtureKey("normalized query", 0, None): model_response,
            PlanningFixtureKey("normalized query", 1, "retry"): model_response,
        }
    )
    initial = planner_for(model, trace, clock, ids).plan(
        state, planning_policy()
    )
    restarted = planner_for(model, trace, clock, ids)
    restarted.restore_trusted_lineage(initial.replan_context)

    result = restarted.replan(
        state,
        prior_result=initial,
        request=ReplanRequest(
            request_id="replan_after_restart",
            run_id=state.run_id,
            context=initial.replan_context,
            reason_code="retry",
            reason="resume durable lineage",
        ),
        planning_policy=planning_policy(),
        replan_policy=ReplanPolicy(max_replans=1),
    )

    assert result.decision.status is ReplanDecisionStatus.APPROVED
    assert result.planning_result is not None
    assert result.planning_result.replan_context.replan_count == 1
