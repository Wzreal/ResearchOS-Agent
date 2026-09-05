from __future__ import annotations

import asyncio

from runtime_fixtures import build_executor, runtime_example

from researchos.adapters.mock_execution import (
    ExecutionFixture,
    ExecutionFixtureKey,
    MockAction,
    MockTaskExecutionBackend,
    successful_result,
)
from researchos.domain.runtime import (
    ExecutionResultStatus,
    ExecutorStatus,
    RuntimePauseReason,
    RuntimeResourceAmount,
    TaskExecutionResult,
    TaskStatus,
    UsageCertainty,
)


class HangingSleeper:
    async def sleep(self, milliseconds: int) -> None:
        del milliseconds
        await asyncio.Future()


def test_initialize_uses_explicit_execution_budget_limits() -> None:
    example = runtime_example()
    backend = MockTaskExecutionBackend({})
    executor, _, store, _, _ = build_executor(example, backend)
    execution_limits = RuntimeResourceAmount(
        duration_milliseconds=2_000,
        tokens=2_000,
        cost_microunits=2_000,
        tool_calls=2,
    )

    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
        budget_limits=execution_limits,
    )

    checkpoint = store.load(example.state.run_id)
    assert checkpoint.budget.limits == execution_limits
    assert checkpoint.budget.consumed == RuntimeResourceAmount()


def success_fixtures() -> dict[ExecutionFixtureKey, ExecutionFixture]:
    usage = RuntimeResourceAmount(
        duration_milliseconds=10, tokens=10, cost_microunits=10
    )
    outputs = {
        "task_a": ("sources",),
        "task_b": ("risks",),
        "task_c": ("comparison",),
    }
    return {
        ExecutionFixtureKey(task_id, 1): ExecutionFixture(
            result=successful_result(usage=usage, output_ids=output_ids)
        )
        for task_id, output_ids in outputs.items()
    }


def test_ready_tasks_execute_concurrently_and_dependencies_wait() -> None:
    example = runtime_example(max_concurrency=2)
    fixtures = {
        key: ExecutionFixture(result=value.result, delay_milliseconds=1)
        for key, value in success_fixtures().items()
    }
    backend = MockTaskExecutionBackend(fixtures)
    executor, _, store, _, _ = build_executor(example, backend)
    executor._sleeper = HangingSleeper()
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )

    summary = asyncio.run(executor.execute(example.state))

    checkpoint = store.load(example.state.run_id)
    assert summary.executor_status is ExecutorStatus.COMPLETED
    assert summary.succeeded == 3
    assert backend.max_active == 2
    assert [item.task_id for item in backend.requests][:2] == ["task_a", "task_b"]
    assert checkpoint.task_states[-1].task_id == "task_c"


def test_failure_blocks_dependents_but_independent_branch_completes() -> None:
    example = runtime_example(max_concurrency=2)
    fixtures = success_fixtures()
    fixtures[ExecutionFixtureKey("task_a", 1)] = ExecutionFixture(
        result=TaskExecutionResult(
            status=ExecutionResultStatus.FAILED,
            failure_code="permanent_failure",
            usage=RuntimeResourceAmount(tokens=1),
            usage_certainty=UsageCertainty.EXACT,
        )
    )
    backend = MockTaskExecutionBackend(fixtures)
    executor, _, store, _, _ = build_executor(example, backend)
    executor._sleeper = HangingSleeper()
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )

    summary = asyncio.run(executor.execute(example.state))

    states = {
        item.task_id: item for item in store.load(example.state.run_id).task_states
    }
    assert summary.executor_status is ExecutorStatus.COMPLETED
    assert (summary.failed, summary.blocked, summary.succeeded) == (1, 1, 1)
    assert states["task_c"].blocked_by == ("task_a",)
    assert states["task_b"].status is TaskStatus.SUCCEEDED


def test_retry_reuses_operation_key_and_changes_attempt_key() -> None:
    example = runtime_example(max_attempts=2)
    fixtures = success_fixtures()
    fixtures[ExecutionFixtureKey("task_a", 1)] = ExecutionFixture(
        result=TaskExecutionResult(
            status=ExecutionResultStatus.FAILED,
            failure_code="transient",
            retryable=True,
            usage=RuntimeResourceAmount(tokens=1),
            usage_certainty=UsageCertainty.EXACT,
        )
    )
    fixtures[ExecutionFixtureKey("task_a", 2)] = ExecutionFixture(
        result=successful_result(
            usage=RuntimeResourceAmount(tokens=2), output_ids=("sources",)
        )
    )
    backend = MockTaskExecutionBackend(fixtures)
    executor, _, store, _, sleeper = build_executor(example, backend)
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )

    summary = asyncio.run(executor.execute(example.state))

    task = next(
        item
        for item in store.load(example.state.run_id).task_states
        if item.task_id == "task_a"
    )
    assert summary.succeeded == 3
    assert len(task.attempts) == 2
    assert task.attempts[0].operation_key == task.attempts[1].operation_key
    assert task.attempts[0].attempt_key != task.attempts[1].attempt_key
    assert len(sleeper.delays) >= 2


def test_timeout_unknown_usage_is_uncertain_consumption() -> None:
    example = runtime_example(timeout_milliseconds=100)
    fixtures = success_fixtures()
    fixtures[ExecutionFixtureKey("task_a", 1)] = ExecutionFixture(
        action=MockAction.NEVER_RETURN
    )
    backend = MockTaskExecutionBackend(fixtures)
    executor, _, store, _, _ = build_executor(example, backend)
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )

    summary = asyncio.run(executor.execute(example.state))

    checkpoint = store.load(example.state.run_id)
    assert summary.failed == 1
    assert checkpoint.budget.uncertain_consumption.duration_milliseconds == 100


def test_run_cancellation_uses_command_and_attempt_signal() -> None:
    async def scenario() -> tuple[ExecutorStatus, MockTaskExecutionBackend]:
        example = runtime_example()
        fixtures = success_fixtures()
        fixtures[ExecutionFixtureKey("task_a", 1)] = ExecutionFixture(
            action=MockAction.WAIT_FOR_CANCELLATION
        )
        fixtures[ExecutionFixtureKey("task_b", 1)] = ExecutionFixture(
            action=MockAction.WAIT_FOR_CANCELLATION
        )
        backend = MockTaskExecutionBackend(fixtures)
        executor, _, _, _, _ = build_executor(example, backend)
        executor._sleeper = HangingSleeper()
        executor.initialize(
            example.state,
            example.dag,
            example.policy,
            replan_context=example.context,
        )
        running = asyncio.create_task(executor.execute(example.state))
        for _ in range(5):
            await asyncio.sleep(0)
        executor.request_cancel()
        summary = await running
        return summary.executor_status, backend

    status, backend = asyncio.run(scenario())
    assert status is ExecutorStatus.CANCELLED
    assert len(backend.cancelled_attempts) == 2


def test_backend_overrun_pauses_after_committing_all_batch_results() -> None:
    example = runtime_example(max_concurrency=2)
    fixtures = success_fixtures()
    fixtures[ExecutionFixtureKey("task_a", 1)] = ExecutionFixture(
        result=successful_result(
            usage=RuntimeResourceAmount(
                duration_milliseconds=4_000_000,
                tokens=200_000,
                cost_microunits=20_000_000,
                tool_calls=200,
            ),
            output_ids=("sources",),
        )
    )
    backend = MockTaskExecutionBackend(fixtures)
    executor, _, store, _, _ = build_executor(example, backend)
    executor._sleeper = HangingSleeper()
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )

    summary = asyncio.run(executor.execute(example.state))

    checkpoint = store.load(example.state.run_id)
    assert summary.executor_status is ExecutorStatus.PAUSED
    assert checkpoint.pause_reason is RuntimePauseReason.BUDGET_BREACHED
    assert checkpoint.budget.consumed.tokens > checkpoint.budget.limits.tokens
    assert (
        next(item for item in checkpoint.task_states if item.task_id == "task_b").status
        is TaskStatus.SUCCEEDED
    )
