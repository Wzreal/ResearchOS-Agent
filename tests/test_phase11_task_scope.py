"""Phase 11 planning bounds each self-contained research task to its tools."""

from __future__ import annotations

from planning_fixtures import candidate_payload, planning_request

from researchos.adapters.deepseek import _messages
from researchos.application.dag_validator import DAGValidator
from researchos.configuration.validation import deepseek_system_prompt
from researchos.domain.planning import CandidatePlan, ValidationIssueCode


def _phase11_request():
    return planning_request(
        allowed_capability_ids=("read", "search", "web_browser", "web_search"),
        per_task_tool_call_limit=3,
    )


def _candidate(request, payload):
    return CandidatePlan.model_validate(payload), request


def test_phase11_prompt_and_request_expose_hard_per_task_tool_budget() -> None:
    request = _phase11_request()
    prompt = deepseek_system_prompt(
        "planning", require_explicit_web_search_capability=True
    )
    messages = _messages("planning", request, planning_model_id="mock_model")

    assert "per_task_tool_call_limit" in prompt
    assert "Prefer one primary evidence target per task" in prompt
    assert "top-N, all, every" in prompt
    assert '"per_task_tool_call_limit":3' in messages[1]["content"]


def test_phase11_top_five_task_estimate_fails_closed_at_tool_budget() -> None:
    request = _phase11_request()
    payload = candidate_payload(request)
    task = payload["tasks"][0]
    task.update(
        objective="Search sources then open the top five returned pages.",
        required_capability_ids=["web_browser", "web_search"],
        estimate={
            "duration_milliseconds": 6_000,
            "tokens": 600,
            "cost_microunits": 600,
            "tool_calls": 6,
        },
    )
    candidate, request = _candidate(request, payload)

    result = DAGValidator().validate(
        candidate,
        request,
        "mock_model",
        per_task_tool_call_limit=request.per_task_tool_call_limit,
    )

    assert not result.valid
    issue = next(
        item
        for item in result.issues
        if item.code is ValidationIssueCode.TASK_TOOL_CALL_LIMIT_EXCEEDED
    )
    assert issue.task_id == "task_a"
    assert issue.details == {"actual": 6, "limit": 3}


def test_phase11_one_page_and_bounded_fallback_tasks_fit_tool_budget() -> None:
    request = _phase11_request()
    payload = candidate_payload(request)
    one_page = payload["tasks"][0]
    one_page.update(
        objective="Search and open one selected authoritative source page.",
        required_capability_ids=["web_browser", "web_search"],
        estimate={
            "duration_milliseconds": 3_000,
            "tokens": 300,
            "cost_microunits": 300,
            "tool_calls": 2,
        },
    )
    fallback = payload["tasks"][1]
    fallback.update(
        objective="Search, open one primary page, then one bounded fallback page.",
        required_capability_ids=["web_browser", "web_search"],
        estimate={
            "duration_milliseconds": 4_000,
            "tokens": 400,
            "cost_microunits": 400,
            "tool_calls": 3,
        },
    )
    candidate, request = _candidate(request, payload)

    result = DAGValidator().validate(
        candidate,
        request,
        "mock_model",
        per_task_tool_call_limit=request.per_task_tool_call_limit,
    )

    assert result.valid
    assert all(task.estimate.tool_calls <= 3 for task in candidate.tasks)


def test_phase11_multisource_work_is_split_into_bounded_self_contained_tasks() -> None:
    request = _phase11_request()
    payload = candidate_payload(request)
    for index, task in enumerate(payload["tasks"], start=1):
        task.update(
            objective=f"Search and open one primary source target {index}.",
            dependencies=[],
            required_capability_ids=["web_browser", "web_search"],
            estimate={
                "duration_milliseconds": 3_000,
                "tokens": 300,
                "cost_microunits": 300,
                "tool_calls": 2,
            },
        )
    candidate, request = _candidate(request, payload)

    result = DAGValidator().validate(
        candidate,
        request,
        "mock_model",
        per_task_tool_call_limit=request.per_task_tool_call_limit,
    )

    assert result.valid
    assert len(candidate.tasks) == 3
    assert all(task.dependencies == () for task in candidate.tasks)
    assert all(task.estimate.tool_calls == 2 for task in candidate.tasks)
