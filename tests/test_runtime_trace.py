import asyncio

from runtime_fixtures import build_executor, runtime_example
from test_async_dag_executor import HangingSleeper, success_fixtures

from researchos.adapters.mock_execution import MockTaskExecutionBackend
from researchos.domain.contracts import TraceEventType


def test_success_settlement_trace_order_is_stable() -> None:
    example = runtime_example(max_concurrency=1)
    backend = MockTaskExecutionBackend(success_fixtures())
    executor, _, _, _, _ = build_executor(example, backend)
    executor._sleeper = HangingSleeper()
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )

    asyncio.run(executor.execute(example.state))

    events = example.trace.read(example.state.run_id)
    types = [event.event_type for event in events]
    attempt_index = types.index(TraceEventType.ATTEMPT_SUCCEEDED)
    assert types[attempt_index : attempt_index + 4] == [
        TraceEventType.ATTEMPT_SUCCEEDED,
        TraceEventType.BUDGET_COMMITTED,
        TraceEventType.BUDGET_RELEASED,
        TraceEventType.TASK_SUCCEEDED,
    ]


def test_runtime_trace_contains_no_backend_payload_contract() -> None:
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
    asyncio.run(executor.execute(example.state))

    persisted = "".join(
        event.model_dump_json() for event in example.trace.read(example.state.run_id)
    )
    assert "runtime query" not in persisted
