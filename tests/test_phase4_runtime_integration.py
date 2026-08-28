from __future__ import annotations

import asyncio

from runtime_fixtures import build_executor, runtime_example

from researchos.adapters.mock_agent import (
    AgentFixture,
    AgentFixtureAction,
    AgentFixtureKey,
    ScriptedAgent,
)
from researchos.application.agent_runner import AgentRunner, AgentRunnerPolicy
from researchos.application.agent_task_backend import AgentTaskExecutionBackend
from researchos.application.capability_registry import CapabilityRegistry
from researchos.domain.agent import (
    AgentDescriptor,
    AgentError,
    AgentFailedDecision,
    AgentFinalDecision,
    AgentFinalResult,
    AgentProducedOutput,
    AgentToolCall,
    AgentToolDecision,
)
from researchos.domain.runtime import (
    ExecutorStatus,
    IdempotencyMode,
    RuntimeResourceAmount,
    TaskStatus,
    UsageCertainty,
)
from researchos.domain.tools import (
    AdapterMode,
    SearchRequest,
    SearchResult,
    ToolDescriptor,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)


class NeverSleeper:
    async def sleep(self, milliseconds: int) -> None:
        del milliseconds
        await asyncio.Future()


class RuntimeSleeper:
    async def sleep(self, milliseconds: int) -> None:
        if milliseconds <= 0:
            await asyncio.sleep(0)
            return
        await asyncio.Future()


class AgentIds:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, prefix: str) -> str:
        self.count += 1
        return f"agent_{prefix}_{self.count}"


class CapabilityTool:
    def __init__(
        self,
        capability_id: str,
        *,
        certainty: UsageCertainty = UsageCertainty.EXACT,
    ) -> None:
        self.requests = []
        self._certainty = certainty
        self._descriptor = ToolDescriptor(
            tool_id=f"{capability_id}_tool",
            capability_id=capability_id,
            adapter_id=f"mock_{capability_id}",
            mode=AdapterMode.MOCK,
            operation_version="v1",
            input_type="search",
            output_type="search_result",
            side_effect=ToolSideEffect.NONE,
            idempotency=IdempotencyMode.IDEMPOTENT,
        )

    @property
    def descriptor(self):
        return self._descriptor

    async def invoke(self, request, cancellation):
        del cancellation
        self.requests.append(request)
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResult(adapter_id=self._descriptor.adapter_id, hits=()),
            usage=(
                ToolUsage(tokens=2, tool_calls=1)
                if self._certainty is not UsageCertainty.UNKNOWN
                else None
            ),
            usage_certainty=self._certainty,
        )


def _final(output_id: str) -> AgentFinalDecision:
    media_types = {
        "sources": "application/json",
        "risks": "application/json",
        "comparison": "text/markdown",
    }
    return AgentFinalDecision(
        final=AgentFinalResult(
            outputs=(
                AgentProducedOutput(
                    output_id=output_id,
                    media_type=media_types.get(output_id, "text/plain"),
                ),
            )
        ),
        usage=RuntimeResourceAmount(tokens=1),
        usage_certainty=UsageCertainty.EXACT,
    )


def _tool_call(capability_id: str) -> AgentToolDecision:
    return AgentToolDecision(
        tool_call=AgentToolCall(
            tool_call_id=f"call_{capability_id}",
            capability_id=capability_id,
            input=SearchRequest(query=f"query for {capability_id}"),
        ),
        usage=RuntimeResourceAmount(tokens=1),
        usage_certainty=UsageCertainty.EXACT,
    )


def _base_fixtures() -> dict[AgentFixtureKey, AgentFixture]:
    return {
        AgentFixtureKey("task_a", 1, 1): AgentFixture(
            decision=_tool_call("search")
        ),
        AgentFixtureKey("task_a", 1, 2): AgentFixture(decision=_final("sources")),
        AgentFixtureKey("task_b", 1, 1): AgentFixture(decision=_final("risks")),
        AgentFixtureKey("task_c", 1, 1): AgentFixture(decision=_final("comparison")),
    }


def _build(example, fixtures, *, search_certainty=UsageCertainty.EXACT):
    agent = ScriptedAgent(
        AgentDescriptor(
            agent_id="integration_agent",
            adapter_id="scripted",
            mode=AdapterMode.MOCK,
            operation_version="v1",
        ),
        fixtures,
    )
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
    search = CapabilityTool("search", certainty=search_certainty)
    registry.register(search)
    registry.register(CapabilityTool("read"))
    runner = AgentRunner(
        agent=agent,
        registry=registry,
        policy=AgentRunnerPolicy(max_agent_steps=3, max_tool_calls=2),
        clock=example.clock,
        sleeper=NeverSleeper(),
        trace_sink=example.trace,
        id_factory=AgentIds(),
    )
    backend = AgentTaskExecutionBackend(runner)
    executor, manager, store, cancellation, sleeper = build_executor(
        example, backend
    )
    executor._sleeper = RuntimeSleeper()
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    return executor, store, search, agent, cancellation, sleeper


def test_phase3_agent_tool_chain_success() -> None:
    example = runtime_example()
    executor, store, search, agent, _, _ = _build(example, _base_fixtures())
    summary = asyncio.run(executor.execute(example.state))
    diagnostic = [
        (
            item.task_id,
            item.status,
            item.attempts[-1].failure_code if item.attempts else None,
        )
        for item in store.load(example.state.run_id).task_states
    ]
    assert summary.executor_status is ExecutorStatus.COMPLETED
    assert summary.succeeded == 3, diagnostic
    assert len(search.requests) == 1
    observed = next(
        request
        for request in agent.requests
        if request.context.task_id == "task_a" and request.agent_step == 2
    )
    assert observed.observations[0].tool_operation_key == (
        search.requests[0].tool_operation_key
    )
    assert all(
        task.status is TaskStatus.SUCCEEDED
        for task in store.load(example.state.run_id).task_states
    )


def test_phase3_agent_failure_and_expected_output_mismatch() -> None:
    example = runtime_example()
    fixtures = _base_fixtures()
    fixtures[AgentFixtureKey("task_a", 1, 1)] = AgentFixture(
        decision=AgentFailedDecision(
            error=AgentError(code="agent_declined", message="declined"),
            usage=RuntimeResourceAmount(tokens=1),
            usage_certainty=UsageCertainty.EXACT,
        )
    )
    executor, store, _, _, _, _ = _build(example, fixtures)
    summary = asyncio.run(executor.execute(example.state))
    states = {
        item.task_id: item
        for item in store.load(example.state.run_id).task_states
    }
    assert summary.failed == 1 and summary.blocked == 1
    assert states["task_a"].attempts[0].failure_code == "agent_declined"

    mismatch_example = runtime_example()
    mismatch = _base_fixtures()
    mismatch[AgentFixtureKey("task_a", 1, 2)] = AgentFixture(
        decision=_final("wrong_output")
    )
    executor, store, _, _, _, _ = _build(mismatch_example, mismatch)
    summary = asyncio.run(executor.execute(mismatch_example.state))
    task_a = next(
        item
        for item in store.load(mismatch_example.state.run_id).task_states
        if item.task_id == "task_a"
    )
    assert summary.failed == 1
    assert task_a.attempts[0].failure_code == "agent_output_contract_violation"

    media_example = runtime_example()
    media_fixtures = _base_fixtures()
    media_fixtures[AgentFixtureKey("task_b", 1, 1)] = AgentFixture(
        decision=AgentFinalDecision(
            final=AgentFinalResult(
                outputs=(
                    AgentProducedOutput(
                        output_id="risks", media_type="text/plain"
                    ),
                )
            ),
            usage=RuntimeResourceAmount(tokens=1),
            usage_certainty=UsageCertainty.EXACT,
        )
    )
    executor, store, _, _, _, _ = _build(media_example, media_fixtures)
    summary = asyncio.run(executor.execute(media_example.state))
    task_b = next(
        item
        for item in store.load(media_example.state.run_id).task_states
        if item.task_id == "task_b"
    )
    assert summary.failed == 1
    assert task_b.attempts[0].failure_code == "agent_output_contract_violation"


def test_phase3_owns_retry_and_tool_identity_survives_attempt_change() -> None:
    example = runtime_example(max_attempts=2)
    fixtures = _base_fixtures()
    fixtures[AgentFixtureKey("task_a", 1, 2)] = AgentFixture(
        decision=AgentFailedDecision(
            error=AgentError(
                code="transient_agent_failure",
                message="retryable",
                retryable=True,
            ),
            usage=RuntimeResourceAmount(tokens=1),
            usage_certainty=UsageCertainty.EXACT,
        )
    )
    fixtures[AgentFixtureKey("task_a", 2, 1)] = AgentFixture(
        decision=_tool_call("search")
    )
    fixtures[AgentFixtureKey("task_a", 2, 2)] = AgentFixture(
        decision=_final("sources")
    )
    executor, store, search, agent, _, _ = _build(example, fixtures)
    summary = asyncio.run(executor.execute(example.state))
    task_a = next(
        item
        for item in store.load(example.state.run_id).task_states
        if item.task_id == "task_a"
    )
    assert summary.succeeded == 3
    assert len(task_a.attempts) == 2
    task_a_attempts = [
        request.context.attempt_number
        for request in agent.requests
        if request.context.task_id == "task_a"
    ]
    assert task_a_attempts == [1, 1, 2, 2]
    assert search.requests[0].attempt_id != search.requests[1].attempt_id
    assert (
        search.requests[0].tool_operation_key
        == search.requests[1].tool_operation_key
    )


def test_phase3_settles_unknown_agent_tool_usage_conservatively() -> None:
    example = runtime_example()
    executor, store, _, _, _, _ = _build(
        example,
        _base_fixtures(),
        search_certainty=UsageCertainty.UNKNOWN,
    )
    summary = asyncio.run(executor.execute(example.state))
    checkpoint = store.load(example.state.run_id)
    task_a = next(item for item in checkpoint.task_states if item.task_id == "task_a")
    assert summary.failed == 1
    assert task_a.attempts[0].usage_certainty is UsageCertainty.UNKNOWN
    assert checkpoint.budget.uncertain_consumption.tool_calls == 2


def test_phase3_cancellation_reaches_agent_decisions() -> None:
    async def scenario():
        example = runtime_example()
        fixtures = _base_fixtures()
        fixtures[AgentFixtureKey("task_a", 1, 1)] = AgentFixture(
            action=AgentFixtureAction.WAIT_FOR_CANCELLATION
        )
        fixtures[AgentFixtureKey("task_b", 1, 1)] = AgentFixture(
            action=AgentFixtureAction.WAIT_FOR_CANCELLATION
        )
        executor, store, _, _, cancellation, _ = _build(example, fixtures)
        running = asyncio.create_task(executor.execute(example.state))
        for _ in range(5):
            await asyncio.sleep(0)
        executor.request_cancel()
        return await running, store, example, cancellation

    summary, store, example, cancellation = asyncio.run(scenario())
    checkpoint = store.load(example.state.run_id)
    assert summary.executor_status is ExecutorStatus.CANCELLED
    assert cancellation.cancellation_requested
    assert all(
        item.status in {TaskStatus.CANCELLED, TaskStatus.BLOCKED}
        for item in checkpoint.task_states
    )
    cancelled_attempts = [
        attempt
        for item in checkpoint.task_states
        if item.status is TaskStatus.CANCELLED
        for attempt in item.attempts
    ]
    assert cancelled_attempts
    assert all(
        attempt.usage_certainty is UsageCertainty.UNKNOWN
        for attempt in cancelled_attempts
    )
    assert checkpoint.budget.uncertain_consumption.tokens > 0
