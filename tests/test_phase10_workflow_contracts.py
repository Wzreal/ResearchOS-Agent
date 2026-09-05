from __future__ import annotations

import pytest
from runtime_fixtures import runtime_example

from researchos.adapters.workflow_handoff_memory import (
    InMemoryWorkflowRuntimeHandoffStore,
)
from researchos.application.workflow_handoff import WorkflowHandoffManager
from researchos.domain.runtime import RuntimeResourceAmount
from researchos.domain.workflow import (
    WorkflowBudgetAllocation,
    WorkflowHandoffStatus,
    WorkflowRuntimeHandoff,
)


def test_workflow_budget_allocation_requires_exact_partition() -> None:
    amount = RuntimeResourceAmount(
        duration_milliseconds=1,
        tokens=1,
        cost_microunits=1,
        tool_calls=1,
    )
    allocation = WorkflowBudgetAllocation(
        total=RuntimeResourceAmount(
            duration_milliseconds=4,
            tokens=4,
            cost_microunits=4,
            tool_calls=4,
        ),
        planning=amount,
        execution=amount,
        claim_extraction=amount,
        verification=amount,
    )

    assert allocation.execution == amount


def test_workflow_budget_allocation_rejects_unallocated_budget() -> None:
    amount = RuntimeResourceAmount(
        duration_milliseconds=1,
        tokens=1,
        cost_microunits=1,
        tool_calls=1,
    )

    with pytest.raises(ValueError, match="exactly partition"):
        WorkflowBudgetAllocation(
            total=RuntimeResourceAmount(
                duration_milliseconds=5,
                tokens=5,
                cost_microunits=5,
                tool_calls=5,
            ),
            planning=amount,
            execution=amount,
            claim_extraction=amount,
            verification=amount,
        )


def test_workflow_handoff_is_separate_cas_bridge() -> None:
    example = runtime_example()
    total = RuntimeResourceAmount(
        duration_milliseconds=example.state.budget.limits.max_duration_seconds * 1_000,
        tokens=example.state.budget.limits.max_tokens,
        cost_microunits=example.state.budget.limits.max_cost_microunits,
        tool_calls=example.state.budget.limits.max_tool_calls,
    )
    allocation = WorkflowBudgetAllocation(
        total=total,
        planning=RuntimeResourceAmount(),
        execution=total,
        claim_extraction=RuntimeResourceAmount(),
        verification=RuntimeResourceAmount(),
    )
    handoff = WorkflowRuntimeHandoff.build(
        handoff_id="handoff_1",
        run_id=example.state.run_id,
        planning_run_revision=example.dag.run_revision,
        runtime_run_revision=example.state.revision,
        dag=example.dag,
        execution_policy=example.policy,
        workflow_profile_hash="a" * 64,
        replan_context=example.context,
        budget_allocation=allocation,
        created_at=example.clock.now(),
    )
    store = InMemoryWorkflowRuntimeHandoffStore()

    store.create(handoff)
    committed = handoff.model_copy(
        update={
            "handoff_revision": 1,
            "status": WorkflowHandoffStatus.CHECKPOINT_COMMITTED,
            "updated_at": example.clock.now(),
        }
    )
    store.save(committed, expected_revision=0)

    assert store.load(example.state.run_id) == committed


def test_prepared_handoff_creates_one_pinned_runtime_checkpoint() -> None:
    example = runtime_example()
    total = RuntimeResourceAmount(
        duration_milliseconds=example.state.budget.limits.max_duration_seconds * 1_000,
        tokens=example.state.budget.limits.max_tokens,
        cost_microunits=example.state.budget.limits.max_cost_microunits,
        tool_calls=example.state.budget.limits.max_tool_calls,
    )
    allocation = WorkflowBudgetAllocation(
        total=total,
        planning=RuntimeResourceAmount(),
        execution=total,
        claim_extraction=RuntimeResourceAmount(),
        verification=RuntimeResourceAmount(),
    )
    handoff = WorkflowRuntimeHandoff.build(
        handoff_id="handoff_2",
        run_id=example.state.run_id,
        planning_run_revision=example.dag.run_revision,
        runtime_run_revision=example.state.revision,
        dag=example.dag,
        execution_policy=example.policy,
        workflow_profile_hash="a" * 64,
        replan_context=example.context,
        budget_allocation=allocation,
        created_at=example.clock.now(),
    )
    handoffs = InMemoryWorkflowRuntimeHandoffStore()
    handoffs.create(handoff)
    from runtime_fixtures import build_executor

    from researchos.adapters.mock_execution import MockTaskExecutionBackend

    executor, _, checkpoints, _, _ = build_executor(
        example, MockTaskExecutionBackend({})
    )
    manager = WorkflowHandoffManager(
        store=handoffs,
        runs=None,  # type: ignore[arg-type]  # already RUNNING in this test
        executor=executor,
        clock=example.clock,
    )

    checkpoint = manager.create_or_validate_checkpoint(example.state)

    assert checkpoints.load(example.state.run_id) == checkpoint
    assert checkpoint.budget.limits == allocation.execution
    assert (
        handoffs.load(example.state.run_id).status
        is WorkflowHandoffStatus.CHECKPOINT_COMMITTED
    )
