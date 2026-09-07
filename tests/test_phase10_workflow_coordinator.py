from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from researchos.application.dag_validator import DAGValidator
from researchos.application.errors import RuntimePreconditionError
from researchos.application.execution_policy_builder import ExecutionPolicyBuilder
from researchos.application.perspective_planner import PerspectivePlanner
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
    PlanningModelResponse,
    PlanningResult,
    PlanningStatus,
    ReplanContext,
)
from researchos.domain.runtime import RuntimeResourceAmount
from researchos.domain.workflow import WorkflowBudgetAllocation


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


def _run_config_for_profile(profile) -> RunConfig:
    total = profile.budget.allocation.total
    return RunConfig(
        mode=OperatingMode.MOCK,
        budget_limits=BudgetLimits(
            max_duration_seconds=total.duration_milliseconds // 1_000,
            max_tokens=total.tokens,
            max_cost_microunits=total.cost_microunits,
            max_tool_calls=total.tool_calls,
        ),
        allowed_capability_ids=("web_search",),
    )


def _phase11_portfolio_execution_profile():
    """The frozen bundle's uniform execution policy, reconstructed offline."""

    profile = build_phase10_mock_bundle_v1().workflow_profile
    allocation = WorkflowBudgetAllocation(
        total=RuntimeResourceAmount(
            duration_milliseconds=1_080_000,
            tokens=147_456,
            cost_microunits=2_266_080,
            tool_calls=15,
        ),
        planning=RuntimeResourceAmount(
            duration_milliseconds=90_000,
            tokens=12_288,
            cost_microunits=163_840,
            tool_calls=1,
        ),
        execution=RuntimeResourceAmount(
            duration_milliseconds=540_000,
            tokens=73_728,
            cost_microunits=1_283_040,
            tool_calls=9,
        ),
        claim_extraction=RuntimeResourceAmount(
            duration_milliseconds=90_000,
            tokens=12_288,
            cost_microunits=163_840,
            tool_calls=1,
        ),
        verification=RuntimeResourceAmount(
            duration_milliseconds=360_000,
            tokens=49_152,
            cost_microunits=655_360,
            tool_calls=4,
        ),
    )
    template = profile.execution.task_policies[0].model_copy(
        update={
            "timeout_milliseconds": 180_000,
            "reservation": RuntimeResourceAmount(
                duration_milliseconds=180_000,
                tokens=24_576,
                cost_microunits=427_680,
                tool_calls=3,
            ),
        }
    )
    return profile.model_copy(
        update={
            "profile_id": "phase11_portfolio_smoke",
            "profile_version": "1",
            "system_commit_sha": "a" * 40,
            "system_version": "phase11-portfolio-smoke-v1",
            "budget": profile.budget.model_copy(
                update={
                    "profile_id": "phase11_portfolio_smoke_budget",
                    "profile_version": "1",
                    "allocation": allocation,
                }
            ),
            "execution": profile.execution.model_copy(
                update={
                    "task_policies": tuple(
                        template.model_copy(update={"task_id": task_id})
                        for task_id in ("task_a", "task_b", "task_c")
                    )
                }
            ),
        }
    )


class DynamicPlanningModel:
    def __init__(self, task_ids: tuple[str, ...]) -> None:
        self.task_ids = task_ids
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return PlanningModelResponse(
            planning_model_id="deepseek-v4-pro",
            payload={
                "schema_version": 1,
                "run_id": request.run_id,
                "plan_id": request.plan_id,
                "perspectives": [
                    {
                        "perspective_id": "dynamic_perspective",
                        "name": "dynamic",
                        "title": "Dynamic",
                        "goal": "Validate dynamic task binding",
                        "rationale": "Offline regression",
                        "priority": 50,
                    }
                ],
                "tasks": [
                    {
                        "task_id": task_id,
                        "perspective_id": "dynamic_perspective",
                        "objective": f"Complete {task_id}",
                        "dependencies": (
                            []
                            if index == 0
                            else [{"task_id": self.task_ids[index - 1]}]
                        ),
                        "required_capability_ids": ["web_search"],
                        "expected_outputs": [
                            {
                                "output_id": f"output_{index}",
                                "description": f"Output {index}",
                                "media_type": "application/json",
                            }
                        ],
                        "estimate": {
                            "duration_milliseconds": 1_000,
                            "tokens": 100,
                            "cost_microunits": 100,
                            "tool_calls": 1,
                        },
                        "policy": {"priority": 50, "required": True},
                    }
                    for index, task_id in enumerate(self.task_ids)
                ],
                "planner_metadata": {
                    "metadata_version": 1,
                    "planning_model_id": "deepseek-v4-pro",
                    "planner_id": "perspective_planner",
                    "planner_version": "1",
                },
            },
        )


class RecordingHandoffs:
    def __init__(self) -> None:
        self.prepared_policy = None
        self.prepared_result = None
        self.checkpoint_calls = 0

    def prepare(self, _state, result, *, execution_policy, **_kwargs) -> None:
        self.prepared_result = result
        self.prepared_policy = execution_policy

    def create_or_validate_checkpoint(self, _state) -> None:
        self.checkpoint_calls += 1


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
    "task_ids",
    (
        ("dynamic_discovery", "dynamic_reconciliation", "dynamic_report"),
        ("dynamic_discovery", "dynamic_reconciliation"),
        (
            "dynamic_discovery",
            "dynamic_reconciliation",
            "dynamic_verification",
            "dynamic_synthesis",
            "dynamic_report",
        ),
    ),
)
def test_phase11_dynamic_planning_rebinds_through_coordinator_handoff(
    task_ids,
    lifecycle,
) -> None:
    runs, _, trace, clock, run_input, _ = lifecycle
    profile = _phase11_portfolio_execution_profile()
    profile_hash = model_sha256(profile)
    state = runs.transition(
        runs.create(run_input, _run_config_for_profile(profile)).run_id,
        RunStatus.PLANNING,
    )
    model = DynamicPlanningModel(task_ids)
    planner = PerspectivePlanner(
        model=model,
        validator=DAGValidator(),
        clock=clock,
        trace_sink=trace,
        id_factory=lambda prefix: f"{prefix}_dynamic",
    )
    handoffs = RecordingHandoffs()
    coordinator = WorkflowCoordinator(
        runs=runs,
        planner=planner,
        execution_policy_builder=ExecutionPolicyBuilder(),
        handoffs=handoffs,
        executor=None,
        evidence_store=None,
        claim_extractor=None,
        verification=None,
        evaluation_harness=None,
        profile=profile,
        clock=clock,
        trace_sink=trace,
        cancellation=NeverCancelled(),
    )

    coordinator._plan_or_recover(state)

    assert len(model.requests) == 1
    assert handoffs.prepared_result.validated_dag is not None
    expected_task_ids = tuple(sorted(task_ids))
    assert tuple(
        task.task_id for task in handoffs.prepared_result.validated_dag.tasks
    ) == expected_task_ids
    assert (
        tuple(item.task_id for item in handoffs.prepared_policy.tasks)
        == expected_task_ids
    )
    assert not {"task_a", "task_b", "task_c"}.intersection(
        item.task_id for item in handoffs.prepared_policy.tasks
    )
    assert handoffs.checkpoint_calls == 1
    assert model_sha256(profile) == profile_hash
    assert profile.budget.allocation.execution.cost_microunits == 1_283_040

    recovered = WorkflowCoordinator(
        runs=runs,
        planner=None,
        execution_policy_builder=None,
        handoffs=None,
        executor=None,
        evidence_store=None,
        claim_extractor=None,
        verification=None,
        evaluation_harness=None,
        profile=profile,
        clock=clock,
        trace_sink=trace,
        cancellation=NeverCancelled(),
    )
    assert recovered._execution_config_for_dag(
        handoffs.prepared_result.validated_dag
    ) == coordinator._execution_config_for_dag(handoffs.prepared_result.validated_dag)


@pytest.mark.parametrize(
    "task_ids",
    (
        ("alpha",),
        ("alpha", "beta"),
        ("alpha", "beta", "gamma"),
        ("alpha", "beta", "gamma", "delta", "epsilon"),
    ),
)
def test_uniform_execution_templates_bind_dynamic_dags_within_planning_capacity(
    task_ids,
    lifecycle,
) -> None:
    runs, _, trace, clock, *_ = lifecycle
    coordinator = _failure_coordinator(runs, trace, clock, planner=None)
    dag = SimpleNamespace(
        tasks=tuple(SimpleNamespace(task_id=item) for item in task_ids)
    )

    effective = coordinator._execution_config_for_dag(dag)
    policy = ExecutionPolicyBuilder().build(
        dag,
        coordinator._profile.budget.allocation.execution,
        effective,
    )

    assert tuple(item.task_id for item in effective.task_policies) == tuple(
        sorted(task_ids)
    )
    assert tuple(item.task_id for item in policy.tasks) == tuple(sorted(task_ids))
    assert coordinator._profile.execution.task_policies != effective.task_policies
    template = coordinator._profile.execution.task_policies[0]
    assert all(
        item.model_dump(mode="python", exclude={"task_id"})
        == template.model_dump(mode="python", exclude={"task_id"})
        for item in effective.task_policies
    )


def test_dynamic_execution_template_rebinding_fails_closed_above_planning_capacity(
    lifecycle,
) -> None:
    runs, _, trace, clock, *_ = lifecycle
    coordinator = _failure_coordinator(runs, trace, clock, planner=None)
    dag = SimpleNamespace(
        tasks=tuple(
            SimpleNamespace(task_id=item)
            for item in (
                "alpha",
                "beta",
                "gamma",
                "delta",
                "epsilon",
                "zeta",
                "eta",
                "theta",
                "iota",
                "kappa",
                "lambda",
            )
        )
    )

    assert coordinator._execution_config_for_dag(dag) is coordinator._profile.execution
    with pytest.raises(RuntimePreconditionError, match="cover DAG exactly"):
        ExecutionPolicyBuilder().build(
            dag,
            coordinator._profile.budget.allocation.execution,
            coordinator._profile.execution,
        )


def test_dynamic_execution_template_rebinding_fails_closed_without_templates(
    lifecycle,
) -> None:
    runs, _, trace, clock, *_ = lifecycle
    coordinator = _failure_coordinator(runs, trace, clock, planner=None)
    coordinator._profile = coordinator._profile.model_copy(
        update={
            "execution": coordinator._profile.execution.model_copy(
                update={"task_policies": ()}
            )
        }
    )
    dag = SimpleNamespace(tasks=(SimpleNamespace(task_id="alpha"),))

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
