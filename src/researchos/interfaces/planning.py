"""Provider-independent planning model port."""

from __future__ import annotations

from typing import Protocol

from researchos.domain.contracts import RunState
from researchos.domain.planning import (
    PlanningModelResponse,
    PlanningPolicy,
    PlanningRequest,
    PlanningResult,
    ReplanContext,
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


class TrustedReplanLineageRestorer(Protocol):
    """Restore only context already validated against a durable checkpoint."""

    def restore_trusted_lineage(self, context: ReplanContext) -> None: ...


class TrustedRuntimeReplanner(TrustedReplanLineageRestorer, Protocol):
    """Consume only a checkpoint-verified runtime replan cursor."""

    def replan_from_trusted_runtime_context(
        self,
        state: RunState,
        *,
        request: ReplanRequest,
        planning_policy: PlanningPolicy,
        replan_policy: ReplanPolicy,
    ) -> ReplanResult: ...
