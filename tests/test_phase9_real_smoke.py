"""Operator-only REAL smoke preflight; never selected by ordinary CI."""

from __future__ import annotations

import os

import pytest
from planning_fixtures import planning_policy

from researchos.adapters.clock import SystemClock
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.adapters.real_composition_filesystem import (
    FilesystemRealCompositionStore,
)
from researchos.application.dag_validator import DAGValidator
from researchos.application.integration_factory import RealIntegrationFactory
from researchos.application.perspective_planner import PerspectivePlanner
from researchos.application.real_composition import RealCompositionManager
from researchos.application.run_manager import RunManager
from researchos.configuration.environment import (
    EnvironmentSecretSource,
    load_real_integration_settings,
)
from researchos.domain.contracts import (
    BudgetLimits,
    OperatingMode,
    RunConfig,
    RunInput,
    RunStatus,
)
from researchos.domain.planning import PlanningStatus


@pytest.mark.requires_real_smoke
def test_real_planning_smoke_requires_acknowledgement_and_live_credentials(tmp_path):
    """Fail closed before any REAL adapter can be constructed.

    The protected workflow supplies the acknowledgement and Environment
    secrets. A single bounded planning call proves dispatch wiring without
    falsely presenting offline mocks as a REAL smoke test.
    """

    if os.environ.get("RESEARCHOS_REAL_SMOKE_ACKNOWLEDGED") != "true":
        pytest.fail("REAL smoke acknowledgement is required")
    settings = load_real_integration_settings()
    secrets = EnvironmentSecretSource()
    for model in settings.models:
        assert secrets.get_secret(model.credential_slot_id) is not None

    clock = SystemClock()
    run_store = FilesystemRunStore(tmp_path)
    compositions = RealCompositionManager(
        settings=settings,
        secrets=secrets,
        store=FilesystemRealCompositionStore(tmp_path, clock=clock),
    )
    runs = RunManager(
        store=run_store,
        trace_sink=FilesystemTraceSink(tmp_path),
        clock=clock,
        integration_guard=compositions,
    )
    state = runs.create(
        RunInput(query="Return a minimal valid research plan for smoke testing."),
        RunConfig(
            mode=OperatingMode.REAL,
            allowed_capability_ids=compositions.configured_capability_ids(),
            budget_limits=BudgetLimits(
                max_tokens=max(
                    item.policy.provider_call_reservation.total_tokens
                    for item in settings.models
                ),
                max_cost_microunits=max(
                    item.policy.provider_call_reservation.cost_microunits
                    for item in settings.models
                ),
            ),
        ),
    )
    state = runs.transition(state.run_id, RunStatus.PLANNING)
    model = RealIntegrationFactory(compositions, run_store=run_store).planning_model(
        state.run_id
    )
    try:
        result = PerspectivePlanner(
            model=model,
            validator=DAGValidator(),
            clock=clock,
            trace_sink=FilesystemTraceSink(tmp_path),
        ).plan(state, planning_policy())
    finally:
        model.close()
    assert result.status is PlanningStatus.VALIDATED
    assert result.validated_dag is not None
