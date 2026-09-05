from __future__ import annotations

import asyncio

from researchos.application.workflow_coordinator import WorkflowCoordinator
from researchos.application.workflow_factory import WorkflowFactory
from researchos.domain.contracts import RunStatus


class NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        await asyncio.Future()


def test_coordinator_returns_terminal_run_without_replaying_authorities(
    lifecycle,
) -> None:
    runs, _, trace, clock, run_input, config = lifecycle
    state = runs.create(run_input, config)
    terminal = runs.finalize(state.run_id, RunStatus.CANCELLED, reason="cancelled")
    coordinator = WorkflowCoordinator(
        runs=runs,
        planner=None,
        execution_policy_builder=None,
        handoffs=None,
        executor=None,
        evidence_store=None,
        claim_extractor=None,
        verification=None,
        evaluation_harness=None,
        profile=None,
        clock=clock,
        trace_sink=trace,
        cancellation=NeverCancelled(),
    )

    assert asyncio.run(coordinator.execute(terminal)) == terminal


def test_filesystem_factory_requires_trace_and_builds_new_phase10_stores(
    tmp_path, lifecycle
) -> None:
    runs, _, trace, clock, *_ = lifecycle
    factory = WorkflowFactory(tmp_path)

    operations = factory.claim_extraction_operations(clock=clock, trace_sink=trace)
    assert operations is not None

    try:
        factory.workflow_handoffs(
            runs=runs, executor=object(), clock=clock, trace_sink=None
        )
    except ValueError as exc:
        assert "trace sink" in str(exc)
    else:
        raise AssertionError("factory accepted a missing trace sink")
