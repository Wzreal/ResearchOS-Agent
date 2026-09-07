from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from planning_fixtures import candidate_payload, planning_policy, planning_request

from researchos.application.dag_validator import DAGValidator
from researchos.domain.contracts import model_sha256
from researchos.domain.planning import (
    CandidatePlan,
    RemainingBudget,
    ValidationIssueCode,
)


def validate_payload(
    payload: dict[str, object], *, request=None, response_model_id: str = "mock_model"
):
    candidate = CandidatePlan.model_validate(payload)
    actual_request = request or planning_request()
    return DAGValidator().validate(candidate, actual_request, response_model_id)


def issue_codes(result) -> set[ValidationIssueCode]:
    return {issue.code for issue in result.issues}


def test_valid_multi_perspective_dag_has_deterministic_metrics_and_order() -> None:
    request = planning_request()
    candidate = CandidatePlan.model_validate(candidate_payload(request))
    validator = DAGValidator()

    result = validator.validate(candidate, request, "mock_model")
    dag = validator.build_dag(
        candidate,
        request,
        result,
        dag_id="dag_test",
        created_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert result.valid
    assert result.topological_order == ("task_a", "task_b", "task_c")
    assert result.graph_depth == 2
    assert result.total_estimate.tokens == 600
    assert result.critical_path_duration_milliseconds == 4_000
    assert [item.perspective_id for item in dag.perspectives] == [
        "perspective_primary",
        "perspective_risk",
    ]


def test_phase11_web_search_directive_requires_explicit_capability() -> None:
    request = planning_request().model_copy(
        update={
            "policy": planning_policy().model_copy(
                update={"require_explicit_web_search_capability": True}
            ),
            "allowed_capability_ids": ("search", "web_search"),
        }
    )
    payload = candidate_payload(request)
    payload["tasks"][0]["objective"] = "Use web search for primary sources"
    payload["tasks"][0]["required_capability_ids"] = []

    result = validate_payload(payload, request=request)

    assert ValidationIssueCode.MISSING_REQUIRED_CAPABILITY in issue_codes(result)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda data: data["tasks"].append(deepcopy(data["tasks"][0])),
            ValidationIssueCode.DUPLICATE_TASK_ID,
        ),
        (
            lambda data: data["perspectives"].append(
                deepcopy(data["perspectives"][0])
            ),
            ValidationIssueCode.DUPLICATE_PERSPECTIVE_ID,
        ),
        (
            lambda data: data["tasks"][0].update(objective="  "),
            ValidationIssueCode.EMPTY_OBJECTIVE,
        ),
        (
            lambda data: data["tasks"][0].update(perspective_id="missing"),
            ValidationIssueCode.UNKNOWN_PERSPECTIVE,
        ),
        (
            lambda data: data["tasks"][0].update(
                dependencies=[{"task_id": "missing"}]
            ),
            ValidationIssueCode.UNKNOWN_DEPENDENCY,
        ),
        (
            lambda data: data["tasks"][0].update(
                dependencies=[{"task_id": "task_a"}]
            ),
            ValidationIssueCode.SELF_DEPENDENCY,
        ),
        (
            lambda data: data["tasks"][2].update(
                dependencies=[{"task_id": "task_a"}, {"task_id": "task_a"}]
            ),
            ValidationIssueCode.DUPLICATE_DEPENDENCY,
        ),
        (
            lambda data: data["tasks"][0].update(
                dependencies=[{"task_id": "task_c"}]
            ),
            ValidationIssueCode.CYCLE_DETECTED,
        ),
        (
            lambda data: data["tasks"][0].update(
                required_capability_ids=["browser"]
            ),
            ValidationIssueCode.UNAUTHORIZED_CAPABILITY,
        ),
        (
            lambda data: data["tasks"][0].update(expected_outputs=[]),
            ValidationIssueCode.INVALID_EXPECTED_OUTPUT,
        ),
        (
            lambda data: data["perspectives"].append(
                {
                    "perspective_id": "perspective_unused",
                    "name": "unused",
                    "title": "Unused",
                    "goal": "Unused goal",
                    "rationale": "Unused rationale",
                    "priority": 1,
                }
            ),
            ValidationIssueCode.ORPHAN_PERSPECTIVE,
        ),
    ],
)
def test_structural_validation_matrix(mutate, expected) -> None:
    request = planning_request()
    payload = candidate_payload(request)
    mutate(payload)
    assert expected in issue_codes(validate_payload(payload, request=request))


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda data: data["tasks"].append(deepcopy(data["tasks"][0])),
            ValidationIssueCode.DUPLICATE_TASK_ID,
        ),
        (
            lambda data: data["tasks"][0].update(task_id="INVALID TASK"),
            ValidationIssueCode.INVALID_TASK_ID,
        ),
        (
            lambda data: data["tasks"][0].update(
                dependencies=[{"task_id": "missing"}]
            ),
            ValidationIssueCode.UNKNOWN_DEPENDENCY,
        ),
        (
            lambda data: data["tasks"][0].update(
                dependencies=[{"task_id": "task_a"}]
            ),
            ValidationIssueCode.SELF_DEPENDENCY,
        ),
        (
            lambda data: data["tasks"][2].update(
                dependencies=[{"task_id": "task_a"}, {"task_id": "task_a"}]
            ),
            ValidationIssueCode.DUPLICATE_DEPENDENCY,
        ),
        (
            lambda data: data["tasks"][0].update(
                dependencies=[{"task_id": "task_c"}]
            ),
            ValidationIssueCode.CYCLE_DETECTED,
        ),
    ],
)
def test_structurally_unsafe_graph_suppresses_metrics_and_duration_budget(
    mutate, expected
) -> None:
    request = planning_request(
        remaining_budget=RemainingBudget(
            duration_milliseconds=0,
            tokens=1,
            cost_microunits=1,
            tool_calls=1,
        )
    )
    payload = candidate_payload(request)
    mutate(payload)
    result = validate_payload(payload, request=request)
    assert expected in issue_codes(result)
    assert result.topological_order == ()
    assert result.graph_depth is None
    assert result.critical_path_duration_milliseconds is None
    assert ValidationIssueCode.BUDGET_DURATION_EXCEEDED not in issue_codes(result)
    assert {
        ValidationIssueCode.BUDGET_TOKENS_EXCEEDED,
        ValidationIssueCode.BUDGET_COST_EXCEEDED,
        ValidationIssueCode.BUDGET_TOOL_CALLS_EXCEEDED,
    } <= issue_codes(result)


def test_semantic_error_keeps_reliable_graph_metrics() -> None:
    request = planning_request(
        remaining_budget=RemainingBudget(
            duration_milliseconds=0,
            tokens=10_000,
            cost_microunits=100_000,
            tool_calls=50,
        )
    )
    payload = candidate_payload(request)
    payload["tasks"][0]["required_capability_ids"] = ["browser"]
    result = validate_payload(payload, request=request)
    assert result.graph_depth == 2
    assert result.topological_order == ("task_a", "task_b", "task_c")
    assert ValidationIssueCode.BUDGET_DURATION_EXCEEDED in issue_codes(result)


def test_output_id_is_explicit_unique_identity() -> None:
    request = planning_request()
    payload = candidate_payload(request)
    payload["tasks"][0]["expected_outputs"].append(
        {
            "output_id": "sources",
            "description": "A different description cannot redefine the ID",
            "media_type": "text/plain",
        }
    )
    result = validate_payload(payload, request=request)
    assert ValidationIssueCode.DUPLICATE_EXPECTED_OUTPUT in issue_codes(result)


def test_policy_task_dependency_depth_and_budget_limits() -> None:
    request = planning_request(
        policy=planning_policy(
            max_tasks=2, max_dependencies_per_task=0, max_graph_depth=1
        ),
        remaining_budget=RemainingBudget(
            duration_milliseconds=1,
            tokens=1,
            cost_microunits=1,
            tool_calls=1,
        ),
    )
    codes = issue_codes(validate_payload(candidate_payload(request), request=request))
    assert {
        ValidationIssueCode.TASK_LIMIT_EXCEEDED,
        ValidationIssueCode.DEPENDENCY_LIMIT_EXCEEDED,
        ValidationIssueCode.DEPTH_LIMIT_EXCEEDED,
        ValidationIssueCode.BUDGET_DURATION_EXCEEDED,
        ValidationIssueCode.BUDGET_TOKENS_EXCEEDED,
        ValidationIssueCode.BUDGET_COST_EXCEEDED,
        ValidationIssueCode.BUDGET_TOOL_CALLS_EXCEEDED,
    } <= codes


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"run_id": "run_other"}, ValidationIssueCode.REQUEST_RUN_MISMATCH),
        ({"plan_id": "plan_other"}, ValidationIssueCode.REQUEST_PLAN_MISMATCH),
    ],
)
def test_request_identity_mismatch_is_a_stable_issue(change, code) -> None:
    request = planning_request()
    result = validate_payload(candidate_payload(request, **change), request=request)
    assert code in issue_codes(result)


def test_planner_provenance_mismatch_is_a_stable_issue() -> None:
    request = planning_request()
    result = validate_payload(
        candidate_payload(request), request=request, response_model_id="other_model"
    )
    assert ValidationIssueCode.PLANNER_PROVENANCE_MISMATCH in issue_codes(result)


def test_issue_order_topology_and_canonical_dag_hash_are_stable() -> None:
    request = planning_request()
    first_payload = candidate_payload(request)
    second_payload = candidate_payload(request)
    extra_output = {
        "output_id": "appendix",
        "description": "Supporting appendix",
        "media_type": "text/markdown",
    }
    first_payload["tasks"][0]["expected_outputs"].append(extra_output)
    second_payload["tasks"][0]["expected_outputs"].insert(0, extra_output)
    second_payload["tasks"].reverse()
    second_payload["perspectives"].reverse()
    validator = DAGValidator()
    first = CandidatePlan.model_validate(first_payload)
    second = CandidatePlan.model_validate(second_payload)
    first_result = validator.validate(first, request, "mock_model")
    second_result = validator.validate(second, request, "mock_model")
    timestamp = datetime(2026, 8, 27, tzinfo=UTC)
    first_dag = validator.build_dag(
        first, request, first_result, dag_id="dag_test", created_at=timestamp
    )
    second_dag = validator.build_dag(
        second, request, second_result, dag_id="dag_test", created_at=timestamp
    )
    assert first_result.topological_order == second_result.topological_order
    assert model_sha256(first_dag) == model_sha256(second_dag)
    assert [
        output.output_id for output in first_dag.tasks[0].expected_outputs
    ] == ["appendix", "sources"]
