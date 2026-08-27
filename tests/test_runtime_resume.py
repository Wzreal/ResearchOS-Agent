from runtime_fixtures import build_executor, runtime_example

from researchos.adapters.mock_execution import MockTaskExecutionBackend
from researchos.domain.runtime import (
    AttemptStatus,
    ExecutionPolicy,
    IdempotencyMode,
    TaskStatus,
    UsageCertainty,
)


def test_running_attempt_becomes_interrupted_and_consumes_attempt_on_resume() -> None:
    example = runtime_example(max_attempts=2)
    executor, _, store, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    task = checkpoint.task_states[0].model_copy(update={"status": TaskStatus.READY})
    checkpoint = executor._mutation(
        checkpoint,
        kind="test_ready",
        task_states=(task,) + checkpoint.task_states[1:],
        event_specs=[],
    )
    checkpoint, first_request = executor._start_attempt(
        checkpoint, task, checkpoint.execution_policy.tasks[0]
    )

    recovered = executor.resume(example.state)
    recovered_task = recovered.task_states[0]

    assert recovered_task.status is TaskStatus.RETRY_WAIT
    assert recovered_task.attempts[0].status is AttemptStatus.INTERRUPTED
    assert recovered_task.attempts[0].usage_certainty is UsageCertainty.UNKNOWN
    assert recovered_task.attempts[0].attempt_number == 1
    assert recovered.budget.uncertain_consumption.duration_milliseconds > 0
    assert store.load(example.state.run_id) == recovered
    assert first_request.operation_key == recovered_task.attempts[0].operation_key


def test_succeeded_outcomes_are_not_redispatched_on_resume() -> None:
    from test_async_dag_executor import HangingSleeper, success_fixtures

    example = runtime_example()
    backend = MockTaskExecutionBackend(success_fixtures())
    executor, _, _, _, _ = build_executor(example, backend)
    executor._sleeper = HangingSleeper()
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )

    import asyncio

    first = asyncio.run(executor.execute(example.state))
    request_count = len(backend.requests)
    second = asyncio.run(executor.execute(example.state))

    assert first == second
    assert len(backend.requests) == request_count


def test_non_idempotent_interrupted_attempt_fails_without_retry() -> None:
    example = runtime_example()
    policies = tuple(
        item.model_copy(
            update={
                "idempotency": IdempotencyMode.NON_IDEMPOTENT,
                "max_attempts": 1,
                "backoff_milliseconds": (),
            }
        )
        for item in example.policy.tasks
    )
    example.policy = ExecutionPolicy(
        max_concurrency=example.policy.max_concurrency,
        tasks=policies,
        max_replans=example.policy.max_replans,
    )
    executor, _, _, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    ready = checkpoint.task_states[0].model_copy(update={"status": TaskStatus.READY})
    checkpoint = executor._mutation(
        checkpoint,
        kind="test_ready_non_idempotent",
        task_states=(ready,) + checkpoint.task_states[1:],
        event_specs=[],
    )
    executor._start_attempt(checkpoint, ready, checkpoint.execution_policy.tasks[0])

    recovered = executor.resume(example.state)

    task = recovered.task_states[0]
    assert task.status is TaskStatus.FAILED
    assert task.attempts[0].status is AttemptStatus.INTERRUPTED
