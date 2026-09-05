"""Narrow Phase 10 pre-planning budget freeze helpers."""

from __future__ import annotations

from researchos.application.errors import PlanningPreconditionError
from researchos.domain.contracts import RunState
from researchos.domain.real_composition import ProviderSuboperationReservation
from researchos.domain.runtime import RuntimeResourceAmount
from researchos.domain.workflow import (
    Phase10PlanningAdmission,
    Phase10WorkflowBudgetConfigV1,
    WorkflowBudgetAllocation,
)


def effective_run_budget(state: RunState) -> RuntimeResourceAmount:
    """Convert the already-persisted effective Run budget without defaults."""

    return RuntimeResourceAmount(
        duration_milliseconds=state.budget.limits.max_duration_seconds * 1_000,
        tokens=state.budget.limits.max_tokens,
        cost_microunits=state.budget.limits.max_cost_microunits,
        tool_calls=state.budget.limits.max_tool_calls,
    )


def materialize_workflow_budget_allocation(
    state: RunState, config: Phase10WorkflowBudgetConfigV1 | None
) -> WorkflowBudgetAllocation:
    """Return the explicit profile allocation only if it pins this Run budget."""

    if config is None:
        raise PlanningPreconditionError(
            "Phase 10 workflow budget configuration is required"
        )
    if config.allocation.total != effective_run_budget(state):
        raise PlanningPreconditionError(
            "Phase 10 workflow allocation differs from effective Run budget"
        )
    return config.allocation


def prepare_phase10_planning_admission(
    state: RunState,
    *,
    config: Phase10WorkflowBudgetConfigV1 | None,
    planning_reservation: ProviderSuboperationReservation | None,
    workflow_profile_hash: str,
    mock_bundle_id: str | None = None,
    mock_bundle_version: str | None = None,
    mock_bundle_hash: str | None = None,
) -> Phase10PlanningAdmission:
    """Materialize exactly one allocation for admission, trace, and handoff."""

    if config is None:
        raise PlanningPreconditionError(
            "Phase 10 workflow budget configuration is required"
        )
    allocation = materialize_workflow_budget_allocation(state, config)
    try:
        return Phase10PlanningAdmission(
            budget_config=config,
            workflow_profile_hash=workflow_profile_hash,
            mock_bundle_id=mock_bundle_id,
            mock_bundle_version=mock_bundle_version,
            mock_bundle_hash=mock_bundle_hash,
            allocation=allocation,
            planning_reservation=planning_reservation,
        )
    except ValueError as exc:
        raise PlanningPreconditionError(str(exc)) from exc
