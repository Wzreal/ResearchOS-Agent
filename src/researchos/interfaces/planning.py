"""Provider-independent planning model port."""

from __future__ import annotations

from typing import Protocol

from researchos.domain.contracts import RunState
from researchos.domain.planning import (
    PlanningModelResponse,
    PlanningPolicy,
    PlanningRequest,
    PlanningResult,
    ReplanPolicy,
    ReplanRequest,
    ReplanResult,
)


class PlanningModel(Protocol):
    """Return one versioned candidate envelope or raise a model failure."""

    def generate(self, request: PlanningRequest) -> PlanningModelResponse: ...


class Planner(Protocol):
    """Application-facing one-shot planning and replan port."""

    def plan(self, state: RunState, policy: PlanningPolicy) -> PlanningResult: ...

    def replan(
        self,
        state: RunState,
        *,
        prior_result: PlanningResult,
        request: ReplanRequest,
        planning_policy: PlanningPolicy,
        replan_policy: ReplanPolicy,
    ) -> ReplanResult: ...
