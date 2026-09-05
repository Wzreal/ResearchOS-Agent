from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from researchos.application.errors import RuntimePreconditionError
from researchos.application.execution_policy_builder import ExecutionPolicyBuilder
from researchos.application.workflow_coordinator import WorkflowCoordinator
from researchos.application.workflow_factory import WorkflowFactory
from researchos.configuration.phase10_mock import build_phase10_mock_bundle_v1
from researchos.domain.contracts import (
    BudgetLimits,
    OperatingMode,
    RunConfig,
    RunStatus,
    model_sha256,
)
from researchos.domain.planning import (
    PlanningError,
    PlanningResult,
    PlanningStatus,
    ReplanContext,
)


class NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        await asyncio.Future()


class FailingPlanner:
    def __init__(self, *, code: str, message: str, retryable: bool) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.calls = 0

    def plan(self, state, *_args, **_kwargs):
        self.calls += 1
        return PlanningResult(
            status=PlanningStatus.MODEL_ERROR,
            run_id=state.run_id,
            run_revision=state.revision,
            plan_id="plan_failure",
            planning_request_id="preq_failure",
            validation=None,
            planning_error=PlanningError(
                code=self.code, message=self.message, retryable=self.retryable
            ),
            replan_context=ReplanContext(
                prior_plan_id="plan_failure", replan_count=0
            ),
        )


def _failure_coordinator(runs, trace, clock, planner) -> WorkflowCoordinator:
    return WorkflowCoordinator(
        runs=runs,
        planner=planner,
        execution_policy_builder=None,
        handoffs=None,
        executor=None,
        evidence_store=None,
        claim_extractor=None,
        verification=None,
        evaluation_harness=None,
        profile=build_phase10_mock_bundle_v1().workflow_profile,
        clock=clock,
        trace_sink=trace,
        cancellation=NeverCancelled(),
    )


def _profile_config() -> RunConfig:
    allocation = build_phase10_mock_bundle_v1().workflow_profile.budget.allocation
    return RunConfig(
        mode=OperatingMode.MOCK,
        budget_limits=BudgetLimits(
            max_duration_seconds=allocation.total.duration_milliseconds // 1_000,
            max_tokens=allocation.total.tokens,
            max_cost_microunits=allocation.total.cost_microunits,
            max_tool_calls=allocation.total.tool_calls,
        ),
    )


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


def test_planning_model_failure_finalizes_valid_failed_run_with_original_error(
    lifecycle,
) -> None:
    runs, _, trace, clock, run_input, _ = lifecycle
    planner = FailingPlanner(
        code="provider_response_incomplete",
        message="planning provider response is incomplete",
        retryable=False,
    )
    state = asyncio.run(
        _failure_coordinator(runs, trace, clock, planner).create_and_execute(
            run_input, _profile_config()
        )
    )

    assert state.status is RunStatus.FAILED
    assert len(state.errors) == 1
    assert state.errors[0].code == "provider_response_incomplete"
    assert state.errors[0].message == "planning provider response is incomplete"
    assert state.errors[0].retryable is False
    assert planner.calls == 1


def test_planning_failure_preserves_dispatch_outcome_semantics(lifecycle) -> None:
    runs, _, trace, clock, run_input, _ = lifecycle
    planner = FailingPlanner(
        code="provider_total_call_timeout",
        message="planning provider outcome is unknown after dispatch",
        retryable=False,
    )
    state = asyncio.run(
        _failure_coordinator(runs, trace, clock, planner).create_and_execute(
            run_input, _profile_config()
        )
    )

    assert state.status is RunStatus.FAILED
    assert state.errors[0].code == "provider_total_call_timeout"
    assert "unknown after dispatch" in state.errors[0].message
    assert planner.calls == 1


def test_uniform_execution_templates_bind_dynamic_validated_dag_ids_and_build_policy(
    lifecycle,
) -> None:
    runs, _, trace, clock, *_ = lifecycle
    coordinator = _failure_coordinator(runs, trace, clock, planner=None)
    dag = SimpleNamespace(
        tasks=tuple(
            SimpleNamespace(task_id=item) for item in ("alpha", "beta", "gamma")
        )
    )
    original = coordinator._profile.execution
    original_profile_hash = model_sha256(coordinator._profile)
    effective = coordinator._execution_config_for_dag(dag)
    recovered = coordinator._execution_config_for_dag(dag)
    policy = ExecutionPolicyBuilder().build(
        dag,
        coordinator._profile.budget.allocation.execution,
        effective,
    )

    assert tuple(item.task_id for item in effective.task_policies) == (
        "alpha",
        "beta",
        "gamma",
    )
    assert tuple(item.task_id for item in policy.tasks) == ("alpha", "beta", "gamma")
    assert effective == recovered
    assert tuple(
        item.model_dump(mode="python", exclude={"task_id"})
        for item in effective.task_policies
    ) == tuple(
        item.model_dump(mode="python", exclude={"task_id"})
        for item in original.task_policies
    )
    assert sum(
        item.reservation.cost_microunits for item in effective.task_policies
    ) == sum(item.reservation.cost_microunits for item in original.task_policies)
    assert (
        coordinator._profile.budget.allocation.execution
        == build_phase10_mock_bundle_v1().workflow_profile.budget.allocation.execution
    )
    assert coordinator._profile.execution == original
    assert model_sha256(coordinator._profile) == original_profile_hash


@pytest.mark.parametrize(
    "task_ids", (("alpha", "beta"), ("alpha", "beta", "gamma", "delta"))
)
def test_dynamic_execution_template_rebinding_fails_closed_for_cardinality_mismatch(
    task_ids,
    lifecycle,
) -> None:
    runs, _, trace, clock, *_ = lifecycle
    coordinator = _failure_coordinator(runs, trace, clock, planner=None)
    dag = SimpleNamespace(
        tasks=tuple(SimpleNamespace(task_id=item) for item in task_ids)
    )

    assert coordinator._execution_config_for_dag(dag) is coordinator._profile.execution
    with pytest.raises(RuntimePreconditionError, match="cover DAG exactly"):
        ExecutionPolicyBuilder().build(
            dag,
            coordinator._profile.budget.allocation.execution,
            coordinator._profile.execution,
        )


def test_dynamic_execution_template_rebinding_fails_closed_for_nonuniform_templates(
    lifecycle,
) -> None:
    runs, _, trace, clock, *_ = lifecycle
    coordinator = _failure_coordinator(runs, trace, clock, planner=None)
    original = coordinator._profile.execution
    nonuniform = original.task_policies[1].model_copy(
        update={
            "timeout_milliseconds": 6_000,
            "reservation": original.task_policies[1].reservation.model_copy(
                update={"duration_milliseconds": 6_000}
            ),
        }
    )
    coordinator._profile = coordinator._profile.model_copy(
        update={
            "execution": original.model_copy(
                update={
                    "task_policies": (
                        original.task_policies[0],
                        nonuniform,
                        original.task_policies[2],
                    )
                }
            )
        }
    )
    dag = SimpleNamespace(
        tasks=tuple(
            SimpleNamespace(task_id=item) for item in ("alpha", "beta", "gamma")
        )
    )

    assert coordinator._execution_config_for_dag(dag) is coordinator._profile.execution
    with pytest.raises(RuntimePreconditionError, match="cover DAG exactly"):
        ExecutionPolicyBuilder().build(
            dag,
            coordinator._profile.budget.allocation.execution,
            coordinator._profile.execution,
        )
