from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from researchos.domain.planning import (
    PlanningModelResponse,
    PlanningPolicy,
    PlanningRequest,
    RemainingBudget,
)


def planning_policy(**overrides: int) -> PlanningPolicy:
    values = {
        "max_tasks": 10,
        "max_graph_depth": 5,
        "max_dependencies_per_task": 3,
    }
    values.update(overrides)
    return PlanningPolicy(**values)


def planning_request(**overrides: Any) -> PlanningRequest:
    values: dict[str, Any] = {
        "request_id": "preq_test",
        "run_id": "run_test",
        "run_revision": 2,
        "plan_id": "plan_test",
        "query": "normalized query",
        "source_policy_id": "sources_primary",
        "output_format": "markdown",
        "requested_at": datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        "allowed_capability_ids": ("search", "read"),
        "remaining_budget": RemainingBudget(
            duration_milliseconds=100_000,
            tokens=10_000,
            cost_microunits=100_000,
            tool_calls=50,
        ),
        "policy": planning_policy(),
    }
    values.update(overrides)
    return PlanningRequest(**values)


def candidate_payload(request: PlanningRequest, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "run_id": request.run_id,
        "plan_id": request.plan_id,
        "perspectives": [
            {
                "perspective_id": "perspective_primary",
                "name": "primary",
                "title": "Primary",
                "goal": "Establish the primary facts",
                "rationale": "Direct evidence is required",
                "priority": 80,
            },
            {
                "perspective_id": "perspective_risk",
                "name": "risk",
                "title": "Risk",
                "goal": "Identify counterevidence",
                "rationale": "Avoid one-sided conclusions",
                "priority": 60,
            },
        ],
        "tasks": [
            {
                "task_id": "task_a",
                "perspective_id": "perspective_primary",
                "objective": "Find primary sources",
                "dependencies": [],
                "required_capability_ids": ["search"],
                "expected_outputs": [
                    {
                        "output_id": "sources",
                        "description": "Primary source records",
                        "media_type": "application/json",
                    }
                ],
                "estimate": {
                    "duration_milliseconds": 1_000,
                    "tokens": 100,
                    "cost_microunits": 200,
                    "tool_calls": 2,
                },
                "policy": {"priority": 80, "required": True},
            },
            {
                "task_id": "task_b",
                "perspective_id": "perspective_risk",
                "objective": "Find counterevidence",
                "dependencies": [],
                "required_capability_ids": ["read"],
                "expected_outputs": [
                    {
                        "output_id": "risks",
                        "description": "Counterevidence and risks",
                        "media_type": "application/json",
                    }
                ],
                "estimate": {
                    "duration_milliseconds": 2_000,
                    "tokens": 200,
                    "cost_microunits": 300,
                    "tool_calls": 1,
                },
                "policy": {"priority": 60, "required": True},
            },
            {
                "task_id": "task_c",
                "perspective_id": "perspective_primary",
                "objective": "Compare the findings",
                "dependencies": [{"task_id": "task_a"}],
                "required_capability_ids": [],
                "expected_outputs": [
                    {
                        "output_id": "comparison",
                        "description": "Comparison of findings",
                        "media_type": "text/markdown",
                    }
                ],
                "estimate": {
                    "duration_milliseconds": 3_000,
                    "tokens": 300,
                    "cost_microunits": 400,
                    "tool_calls": 0,
                },
                "policy": {"priority": 50, "required": True},
            },
        ],
        "planner_metadata": {
            "metadata_version": 1,
            "planning_model_id": "mock_model",
            "planner_id": "perspective_planner",
            "planner_version": "2.0",
        },
    }
    values.update(deepcopy(overrides))
    return values


def model_response(request: PlanningRequest, **overrides: Any) -> PlanningModelResponse:
    return PlanningModelResponse(
        planning_model_id="mock_model",
        payload=candidate_payload(request, **overrides),
    )
