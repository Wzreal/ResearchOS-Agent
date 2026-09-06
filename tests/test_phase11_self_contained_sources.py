"""Phase 11 self-contained research-source contract.

Final-20 proved that dependency edges order execution only: upstream task
outputs are never delivered to a downstream task, so a web_browser-only task
that depends on an upstream web_search task has no concrete candidate URLs and
must guess them. These regressions pin the Phase 11 contract that makes source
discovery task-local:

- a task that discovers and reads sources declares web_search + web_browser and
  searches, selects a concrete returned URL, and browses it in the same task;
- a web_browser-only task is valid only when its own objective names a concrete
  HTTP(S) URL.
"""

from __future__ import annotations

from planning_fixtures import candidate_payload, planning_policy, planning_request

from researchos.application.dag_validator import DAGValidator
from researchos.configuration.validation import (
    deepseek_response_contract,
    deepseek_system_prompt,
)
from researchos.domain.planning import (
    CandidatePlan,
    RemainingBudget,
    ValidationIssueCode,
)

_PRIMARY = {
    "perspective_id": "perspective_primary",
    "name": "primary",
    "title": "Primary",
    "goal": "Establish the primary facts",
    "rationale": "Direct evidence is required",
    "priority": 80,
}


def _request():
    return planning_request(
        allowed_capability_ids=("search", "read", "web_search", "web_browser"),
        remaining_budget=RemainingBudget(
            duration_milliseconds=200_000,
            tokens=20_000,
            cost_microunits=200_000,
            tool_calls=100,
        ),
        policy=planning_policy(
            max_tasks=10,
            max_graph_depth=5,
            max_dependencies_per_task=3,
            require_explicit_web_search_capability=True,
        ),
    )


def _task(task_id: str, objective: str, caps: list[str], deps: tuple[str, ...] = ()):
    return {
        "task_id": task_id,
        "perspective_id": "perspective_primary",
        "objective": objective,
        "dependencies": [{"task_id": dep} for dep in deps],
        "required_capability_ids": caps,
        "expected_outputs": [
            {
                "output_id": f"out_{task_id}",
                "description": f"{task_id} output",
                "media_type": "application/json",
            }
        ],
        "estimate": {
            "duration_milliseconds": 1_000,
            "tokens": 100,
            "cost_microunits": 200,
            "tool_calls": 2,
        },
        "policy": {"priority": 50, "required": True},
    }


def _validate(tasks):
    request = _request()
    payload = candidate_payload(request)
    payload["perspectives"] = [_PRIMARY]
    payload["tasks"] = tasks
    candidate = CandidatePlan.model_validate(payload)
    return DAGValidator().validate(candidate, request, "mock_model")


def _codes(result) -> set[ValidationIssueCode]:
    return {issue.code for issue in result.issues}


def test_phase11_browser_only_depending_on_upstream_search_is_rejected() -> None:
    """Final-20 shape: a browser-only task that reads 'candidates from t1/t2'."""
    result = _validate(
        [
            _task("t1", "Locate NASA source pages", ["web_search"]),
            _task("t2", "Locate additional NASA source pages", ["web_search"]),
            _task(
                "t3",
                "Open and read each candidate page from t1 and t2 and extract "
                "the page URL and crew names",
                ["web_browser"],
                deps=["t1", "t2"],
            ),
        ]
    )
    assert not result.valid
    assert ValidationIssueCode.BROWSER_TASK_REQUIRES_LOCAL_URL in _codes(result)


def test_phase11_self_contained_search_and_browser_task_is_valid() -> None:
    """A task that discovers and reads sources declares both capabilities."""
    result = _validate(
        [
            _task(
                "t1",
                "Discover authoritative source pages and read the selected "
                "returned URLs",
                ["web_search", "web_browser"],
            )
        ]
    )
    assert result.valid
    assert not _codes(result)


def test_phase11_browser_only_with_explicit_objective_url_is_valid() -> None:
    result = _validate(
        [
            _task(
                "t1",
                "Open https://www.example.org/source and extract the facts",
                ["web_browser"],
            )
        ]
    )
    assert result.valid
    assert not _codes(result)


def test_phase11_browser_only_without_url_and_without_search_is_rejected() -> None:
    result = _validate(
        [
            _task(
                "t1",
                "Open the announcement page and extract the crew names",
                ["web_browser"],
            )
        ]
    )
    assert not result.valid
    assert ValidationIssueCode.BROWSER_TASK_REQUIRES_LOCAL_URL in _codes(result)


def test_phase11_planning_contract_is_scoped_and_legacy_prompt_is_unchanged() -> None:
    legacy_prompt = deepseek_system_prompt("planning")
    phase11_prompt = deepseek_system_prompt(
        "planning", require_explicit_web_search_capability=True
    )
    assert "TaskDependency edges define execution ordering only" in phase11_prompt
    assert "self-contained" in phase11_prompt
    assert "concrete HTTP(S) source URL(s)" in phase11_prompt
    assert '"web_search"' in phase11_prompt
    assert '"web_browser"' in phase11_prompt
    assert "TaskDependency edges define execution ordering only" not in legacy_prompt
    assert "concrete HTTP(S) source URL(s)" not in legacy_prompt
    # Phase 9 planning contract identity is untouched by the Phase 11-only edit.
    assert deepseek_response_contract("planning") == "planning-model-response-v1"
