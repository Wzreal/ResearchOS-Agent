from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from researchos.adapters.browser import (
    BoundedBrowserTool,
    BrowserTransportError,
)
from researchos.adapters.memory import InMemoryTraceSink
from researchos.adapters.tavily import TavilySearchTool
from researchos.application.agent_runner import AgentRunner, AgentRunnerPolicy
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.real_composition import BoundRealCapability
from researchos.configuration.real_settings import (
    default_browser_capability,
    default_tavily_capability,
)
from researchos.domain.agent import (
    AgentContext,
    AgentDescriptor,
    AgentFinalDecision,
    AgentFinalResult,
    AgentToolCall,
    AgentToolDecision,
)
from researchos.domain.contracts import TraceEventType
from researchos.domain.planning import ResearchTask
from researchos.domain.real_composition import ProviderSuboperationReservation
from researchos.domain.runtime import (
    IdempotencyMode,
    RuntimeResourceAmount,
    UsageCertainty,
)
from researchos.domain.tools import (
    AdapterMode,
    BrowserRequest,
    SearchRequest,
    SearchResultV2,
    ToolDescriptor,
    ToolError,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)
from researchos.interfaces.providers import ProviderAdmissionProfile
from researchos.interfaces.web import BoundedHttpResponse


class Clock:
    current = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)

    def now(self):
        return self.current


class Signal:
    cancelled = False

    async def wait(self):
        await asyncio.Event().wait()


class Sleeper:
    async def sleep(self, milliseconds):
        await asyncio.Future()


def _reservation(*, cost=0, tokens=0, tool_calls=0):
    from researchos.domain.identity import stable_hash

    rules = stable_hash({"test": 1})
    return ProviderSuboperationReservation.build(
        reservation_version="test-reservation-v1",
        duration_milliseconds=1_000,
        tokens=tokens,
        cost_microunits=cost,
        cost_currency="USD",
        tool_calls=tool_calls,
        pricing_policy_id="test_policy",
        pricing_policy_version="v1",
        pricing_rules_hash=rules,
    )


class ReservedAgent:
    def __init__(self, decisions, reservation=None):
        self.decisions = list(decisions)
        self.calls = 0
        self._reservation = reservation or _reservation()
        self._descriptor = AgentDescriptor(
            agent_id="real_agent",
            adapter_id="real_agent_adapter",
            mode=AdapterMode.REAL,
            operation_version="v1",
        )

    @property
    def descriptor(self):
        return self._descriptor

    @property
    def provider_call_reservation(self):
        return self._reservation

    async def decide(self, request, cancellation):
        del request, cancellation
        self.calls += 1
        return self.decisions.pop(0)


class LegacyDirectAgent(ReservedAgent):
    """A Phase 9A binding with no Phase 9B suboperation timeout."""

    @property
    def provider_admission_profile(self):
        return ProviderAdmissionProfile.LEGACY_TASK_LIMIT_V1

    @property
    def provider_call_reservation(self):
        raise AssertionError("legacy profile must not read provider reservation")


class ReservedTool:
    def __init__(
        self,
        *,
        reservation=None,
        retryable_failure=False,
        failure_code: str | None = None,
        delegates_retry_to_phase3: bool = False,
    ):
        self.calls = []
        self._reservation = reservation or _reservation(tool_calls=1)
        self.retryable_failure = retryable_failure
        self.failure_code = failure_code
        self.delegates_retry_to_phase3 = delegates_retry_to_phase3
        self._descriptor = ToolDescriptor(
            tool_id="search_tool",
            capability_id="web_search",
            adapter_id="real_search",
            mode=AdapterMode.REAL,
            operation_version="real-search-v1",
            input_type="search",
            output_type="search_result_v2",
            side_effect=ToolSideEffect.EXTERNAL,
            idempotency=IdempotencyMode.IDEMPOTENT,
        )

    @property
    def descriptor(self):
        return self._descriptor

    @property
    def provider_call_reservation(self):
        return self._reservation

    async def invoke_authorized(self, envelope, cancellation):
        del cancellation
        self.calls.append(envelope)
        if self.retryable_failure or self.failure_code is not None:
            return ToolInvocationResult(
                status=ToolInvocationStatus.FAILED,
                error=ToolError(
                    code=self.failure_code or "search_transient",
                    message="temporary",
                    retryable=self.retryable_failure,
                ),
                usage=ToolUsage(tool_calls=1),
                usage_certainty=UsageCertainty.EXACT,
            )
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResultV2(adapter_id="real_search", hits=()),
            usage=ToolUsage(tool_calls=1),
            usage_certainty=UsageCertainty.EXACT,
        )


def _tool_decision():
    return AgentToolDecision(
        tool_call=AgentToolCall(
            tool_call_id="call_one",
            capability_id="web_search",
            input=SearchRequest(query="query"),
        ),
        usage=RuntimeResourceAmount(cost_microunits=70),
        usage_certainty=UsageCertainty.UPPER_BOUND,
    )


def _browser_decision():
    return AgentToolDecision(
        tool_call=AgentToolCall(
            tool_call_id="call_one",
            capability_id="web_browser",
            input=BrowserRequest(url="https://example.com/"),
        ),
        usage=RuntimeResourceAmount(cost_microunits=70),
        usage_certainty=UsageCertainty.UPPER_BOUND,
    )


def _final_decision():
    return AgentFinalDecision(
        final=AgentFinalResult(outputs=()),
        usage=RuntimeResourceAmount(cost_microunits=70),
        usage_certainty=UsageCertainty.UPPER_BOUND,
    )


def _context(
    *, attempt=1, cost=1_000, tool_calls=2, capability_id="web_search"
):
    task = ResearchTask(
        task_id="task_one",
        perspective_id="perspective_one",
        objective="research",
        expected_outputs=(),
        required_capability_ids=(capability_id,),
    )
    return AgentContext(
        run_id="run_real",
        run_revision=4,
        dag_id="dag_one",
        task=task,
        task_id=task.task_id,
        attempt_id=f"attempt_{attempt}",
        attempt_number=attempt,
        task_operation_key="a" * 64,
        task_attempt_key=("b" if attempt == 1 else "c") * 64,
        task_idempotency=IdempotencyMode.IDEMPOTENT,
        deadline=Clock.current + timedelta(minutes=5),
        hard_limits=RuntimeResourceAmount(
            duration_milliseconds=300_000,
            tokens=10_000,
            cost_microunits=cost,
            tool_calls=tool_calls,
        ),
        expected_outputs=(),
        authorized_capability_ids=(capability_id,),
    )


def _run(agent, tool, context, *, trace=None):
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.REAL}))
    registry.register(tool)
    return asyncio.run(
        AgentRunner(
            agent=agent,
            registry=registry,
            policy=AgentRunnerPolicy(max_agent_steps=3, max_tool_calls=2),
            clock=Clock(),
            sleeper=Sleeper(),
            trace_sink=trace or InMemoryTraceSink(),
        ).run(context, Signal())
    )


def test_second_agent_call_is_denied_when_reservation_no_longer_fits() -> None:
    agent = ReservedAgent(
        [_tool_decision(), _final_decision()], reservation=_reservation(cost=950)
    )
    tool = ReservedTool()
    result = _run(agent, tool, _context(cost=1_000))
    assert result.error is not None
    assert result.error.code == "agent_provider_reservation_exceeds_remaining_cost"
    assert agent.calls == 1
    assert len(tool.calls) == 1


def test_legacy_direct_agent_skips_phase9b_reservation_property() -> None:
    agent = LegacyDirectAgent([_final_decision()])
    result = _run(agent, ReservedTool(), _context())
    assert result.status.value == "succeeded"
    assert agent.calls == 1


def test_tool_reservation_denial_makes_zero_tool_calls() -> None:
    agent = ReservedAgent([_tool_decision()])
    tool = ReservedTool(reservation=_reservation(cost=100, tool_calls=1))
    result = _run(agent, tool, _context(cost=100))
    assert result.error is not None
    assert result.error.code == "tool_provider_reservation_exceeds_remaining_cost"
    assert tool.calls == []


def test_sufficient_reservations_permit_exactly_one_tool_call() -> None:
    agent = ReservedAgent([_tool_decision(), _final_decision()])
    tool = ReservedTool(reservation=_reservation(cost=100, tool_calls=1))
    result = _run(agent, tool, _context(cost=1_000))
    assert result.status.value == "succeeded"
    assert len(tool.calls) == 1


def test_local_retryable_tool_failure_remains_an_agent_observation() -> None:
    agent = ReservedAgent([_tool_decision(), _final_decision()])
    tool = ReservedTool(retryable_failure=True)
    result = _run(agent, tool, _context())
    assert result.status.value == "succeeded"
    assert len(result.observations) == 1
    assert result.observations[0].result.error.code == "search_transient"
    assert agent.calls == 2
    assert len(tool.calls) == 1


def test_explicit_phase3_retry_delegating_tool_terminates_agent_attempt() -> None:
    agent = ReservedAgent([_tool_decision(), _final_decision()])
    tool = ReservedTool(
        retryable_failure=True,
        delegates_retry_to_phase3=True,
    )
    result = _run(agent, tool, _context())
    assert result.error is not None and result.error.code == "search_transient"
    assert result.error.retryable is True
    assert agent.calls == 1
    assert len(tool.calls) == 1


def test_failed_tool_call_consumes_limit_and_cannot_be_bypassed() -> None:
    agent = ReservedAgent([_tool_decision(), _tool_decision(), _final_decision()])
    tool = ReservedTool(failure_code="search_semantic_failure")

    result = _run(agent, tool, _context(tool_calls=1))

    assert result.error is not None
    assert result.error.code == "agent_tool_call_limit_exceeded"
    assert agent.calls == 2
    assert len(tool.calls) == 1


def test_tool_coroutine_is_not_created_before_start_trace_commits() -> None:
    class FailOnToolStart(InMemoryTraceSink):
        def append(self, event):
            if event.event_type is TraceEventType.TOOL_INVOCATION_STARTED:
                raise OSError("trace fault")
            super().append(event)

    agent = ReservedAgent([_tool_decision()])
    tool = ReservedTool()

    result = _run(agent, tool, _context(), trace=FailOnToolStart())

    assert result.error is not None
    assert result.error.code == "tool_trace_start_failed"
    assert tool.calls == []


def test_tavily_429_makes_one_http_call_and_returns_retry_to_phase3() -> None:
    class Authorizer:
        def authorize(self, envelope):
            del envelope

    class Secrets:
        def capability_secret(self, capability_id):
            assert capability_id == "web_search"
            return "test-credential"

    class Transport:
        calls = 0

        async def post_json(self, **values):
            del values
            self.calls += 1
            return BoundedHttpResponse(status_code=429, headers=(), body=b"")

    settings = default_tavily_capability()
    transport = Transport()
    tool = TavilySearchTool(
        bound=BoundRealCapability(
            run_id="run_real",
            run_config_hash="a" * 64,
            composition_hash="b" * 64,
            settings=settings,
        ),
        authorizer=Authorizer(),
        compositions=Secrets(),
        transport=transport,
        clock=Clock(),
    )
    agent = ReservedAgent([_tool_decision(), _final_decision()])

    result = _run(agent, tool, _context(cost=1_000_000))

    assert result.error is not None
    assert result.error.code == "tavily_http_429"
    assert result.error.retryable is True
    assert transport.calls == 1
    assert agent.calls == 1


def test_browser_transient_failure_does_not_retry_inside_agent_attempt() -> None:
    class Authorizer:
        def authorize(self, envelope):
            del envelope

    class Transport:
        calls = 0

        async def get(self, **values):
            del values
            self.calls += 1
            raise BrowserTransportError(
                "browser_dns_failed", dispatched=False, retryable=True
            )

    class Extractor:
        async def extract(self, content, **values):
            raise AssertionError("extraction must not run")

    settings = default_browser_capability()
    transport = Transport()
    tool = BoundedBrowserTool(
        bound=BoundRealCapability(
            run_id="run_real",
            run_config_hash="a" * 64,
            composition_hash="b" * 64,
            settings=settings,
        ),
        authorizer=Authorizer(),
        transport=transport,
        extractor=Extractor(),
        clock=Clock(),
    )
    agent = ReservedAgent([_browser_decision(), _final_decision()])

    result = _run(
        agent,
        tool,
        _context(cost=1_000_000, capability_id="web_browser"),
    )

    assert result.error is not None
    assert result.error.code == "browser_dns_failed"
    assert result.error.retryable is True
    assert transport.calls == 1
    assert agent.calls == 1


def test_tool_logical_identity_is_stable_across_phase3_attempts() -> None:
    operation_keys = []
    attempt_ids = []
    for attempt in (1, 2):
        agent = ReservedAgent([_tool_decision(), _final_decision()])
        tool = ReservedTool()
        result = _run(agent, tool, _context(attempt=attempt))
        assert result.status.value == "succeeded"
        operation_keys.append(tool.calls[0].invocation.tool_operation_key)
        attempt_ids.append(tool.calls[0].invocation.attempt_id)
    assert operation_keys[0] == operation_keys[1]
    assert attempt_ids[0] != attempt_ids[1]


def test_authorized_real_tool_has_no_direct_v1_invoke_boundary() -> None:
    assert not hasattr(ReservedTool(), "invoke")
