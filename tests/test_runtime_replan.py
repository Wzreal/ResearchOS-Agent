import pytest
from conftest import SequentialIds
from planning_fixtures import model_response, planning_policy
from runtime_fixtures import build_executor, runtime_example

from researchos.adapters.mock_execution import MockTaskExecutionBackend
from researchos.adapters.mock_planning import MockPlanningModel, PlanningFixtureKey
from researchos.application.dag_validator import DAGValidator
from researchos.application.errors import (
    CheckpointCompatibilityError,
    ReplanLineageConflict,
    RuntimePreconditionError,
)
from researchos.application.perspective_planner import PerspectivePlanner
from researchos.application.replan_lineage_recovery import ReplanLineageRecovery
from researchos.domain.planning import ReplanContext, ReplanDecisionStatus, ReplanPolicy
from researchos.domain.runtime import ExecutorStatus, RuntimePauseReason


def test_replan_request_consumes_durable_slot_and_pauses() -> None:
    example = runtime_example(max_replans=1)
    executor, _, store, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )

    request = executor.request_replan(
        example.state, reason_code="invalid_branch", reason="repair graph"
    )

    checkpoint = store.load(example.state.run_id)
    assert checkpoint.executor_status is ExecutorStatus.PAUSED
    assert checkpoint.pause_reason is RuntimePauseReason.REPLAN_REQUESTED
    assert checkpoint.replan.slots_used == 1
    assert checkpoint.replan.pending_request == request
    with pytest.raises(RuntimePreconditionError):
        executor.request_replan(
            example.state, reason_code="again", reason="must not fork"
        )
    assert store.load(example.state.run_id).replan.slots_used == 1


def test_runtime_resume_requires_exact_run_revision() -> None:
    example = runtime_example()
    executor, _, _, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    changed = example.state.model_copy(update={"revision": example.state.revision + 1})
    with pytest.raises(CheckpointCompatibilityError, match="revision"):
        executor.resume(changed)


def test_pending_replan_restores_only_through_validated_checkpoint() -> None:
    example = runtime_example(max_replans=1)
    executor, manager, _, _, _ = build_executor(
        example, MockTaskExecutionBackend({})
    )
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    pending = executor.request_replan(
        example.state, reason_code="repair", reason="repair graph"
    )

    class CapturingRestorer:
        def __init__(self) -> None:
            self.contexts = []

        def restore_trusted_lineage(self, context) -> None:
            self.contexts.append(context)

    restorer = CapturingRestorer()
    recovered = ReplanLineageRecovery(
        checkpoints=manager, restorer=restorer
    ).restore(example.state)

    assert recovered == pending
    assert restorer.contexts == [example.context]


def test_trusted_planner_lineage_restoration_is_idempotent() -> None:
    planner = object.__new__(PerspectivePlanner)
    planner._lineages = {}
    context = ReplanContext(prior_plan_id="plan_a", replan_count=1)

    planner.restore_trusted_lineage(context)
    planner.restore_trusted_lineage(context)

    assert planner._lineages["plan_a"] == context
    with pytest.raises(ReplanLineageConflict):
        planner.restore_trusted_lineage(
            ReplanContext(prior_plan_id="plan_a", replan_count=2)
        )


def test_fresh_planner_consumes_checkpoint_verified_runtime_replan() -> None:
    example = runtime_example(max_replans=1)
    executor, manager, _, _, _ = build_executor(
        example, MockTaskExecutionBackend({})
    )
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    executor.request_replan(
        example.state, reason_code="repair", reason="repair runtime graph"
    )
    model = MockPlanningModel(
        {
            PlanningFixtureKey("runtime query", 1, "repair"): model_response,
        }
    )
    restarted = PerspectivePlanner(
        model=model,
        validator=DAGValidator(),
        clock=example.clock,
        trace_sink=example.trace,
        id_factory=SequentialIds(),
    )
    pending = ReplanLineageRecovery(
        checkpoints=manager, restorer=restarted
    ).restore(example.state)

    result = restarted.replan_from_trusted_runtime_context(
        example.state,
        request=pending,
        planning_policy=planning_policy(),
        replan_policy=ReplanPolicy(max_replans=1),
    )

    assert result.decision.status is ReplanDecisionStatus.APPROVED
    assert result.planning_result is not None
    assert result.planning_result.run_revision == example.state.revision
    assert result.planning_result.replan_context.replan_count == 1
    assert len(model.requests) == 1
    repeated = restarted.replan_from_trusted_runtime_context(
        example.state,
        request=pending.model_copy(update={"request_id": "runtime_repeat"}),
        planning_policy=planning_policy(),
        replan_policy=ReplanPolicy(max_replans=1),
    )
    assert repeated.decision.status is ReplanDecisionStatus.REJECTED_CONTEXT
    assert len(model.requests) == 1
