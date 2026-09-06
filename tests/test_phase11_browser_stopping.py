"""Final-19-style web_browser stopping regressions.

These cases pin the intended browser-stage stopping semantics on the runtime:

Case A  authoritative BrowserResult/source excerpt in hand -> the next Agent
        decision is FINAL; no further browser call is made.
Case B  first BrowserResult is insufficient with a concrete bounded unresolved
        gap -> exactly one additional browser decision is allowed; the second
        BrowserResult is sufficient -> the next decision is FINAL.
Case C  a pathological agent that never finalizes after browser results is
        still stopped fail-closed by the hard tool-call guard
        (agent_tool_call_limit_exceeded), never by increasing the limits.

The Agent under test emulates a model that follows the Phase 11 v4 stopping
contract: it returns FINAL as soon as an authoritative BrowserResult supports
the task objective and otherwise issues only bounded, gap-qualified browser
calls. The tool-call accounting below is what a compliant trajectory must
consume.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from researchos.adapters.memory import InMemoryTraceSink
from researchos.application.agent_runner import AgentRunner, AgentRunnerPolicy
from researchos.application.capability_registry import CapabilityRegistry
from researchos.domain.agent import (
    AgentContext,
    AgentDescriptor,
    AgentFinalDecision,
    AgentFinalResult,
    AgentRequest,
)
from researchos.domain.identity import sha256_text
from researchos.domain.planning import ResearchTask
from researchos.domain.real_composition import ProviderSuboperationReservation
from researchos.domain.real_tools import (
    EvidenceGapCategory,
    Phase11RealWebAgentToolDecision,
    RealWebAgentToolCall,
)
from researchos.domain.runtime import (
    IdempotencyMode,
    RuntimeResourceAmount,
    UsageCertainty,
)
from researchos.domain.tools import (
    AdapterMode,
    BrowserRequest,
    BrowserResult,
    ToolDescriptor,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)
from researchos.interfaces.providers import ProviderAdmissionProfile

_AUTHORITATIVE_URL = "https://authority.test/source"
_AUTHORITATIVE_CONTENT = "AUTHORITATIVE source excerpt answering the objective."
_INSUFFICIENT_URL = "https://partial.test/source"
_INSUFFICIENT_CONTENT = (
    "This page only repeats the headline. AUTHORITATIVE fact missing."
)


class Clock:
    current = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

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

    return ProviderSuboperationReservation.build(
        reservation_version="test-reservation-v1",
        duration_milliseconds=1_000,
        tokens=tokens,
        cost_microunits=cost,
        cost_currency="USD",
        tool_calls=tool_calls,
        pricing_policy_id="test_policy",
        pricing_policy_version="v1",
        pricing_rules_hash=stable_hash({"phase11_browser_stopping": 1}),
    )


class BrowserFakeTool:
    """Authorized REAL web_browser that returns per-URL typed BrowserResult."""

    def __init__(self, *, reservation=None) -> None:
        self.calls: list[object] = []
        self._reservation = reservation or _reservation(tool_calls=1)
        self._descriptor = ToolDescriptor(
            tool_id="browser_tool",
            capability_id="web_browser",
            adapter_id="real_browser",
            mode=AdapterMode.REAL,
            operation_version="real-browser-v1",
            input_type="browser",
            output_type="browser_result",
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
        url = envelope.invocation.input.url
        content = (
            _AUTHORITATIVE_CONTENT
            if url == _AUTHORITATIVE_URL
            else _INSUFFICIENT_CONTENT
        )
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=BrowserResult(
                adapter_id=self._descriptor.adapter_id,
                final_url=url,
                title="Source document",
                content=content,
                content_hash=sha256_text(content),
                retrieved_at=Clock.current,
            ),
            usage=ToolUsage(tool_calls=1),
            usage_certainty=UsageCertainty.EXACT,
        )


class StoppingAgent:
    """Compliant agent that finalizes when an authoritative BrowserResult exists.

    mode:
      "case_a"  one browser call, then FINAL on the authoritative excerpt.
      "case_b"  one insufficient browser call, one additional browser call that
                returns the authoritative excerpt, then FINAL.
      "case_c"  pathological: always requests web_browser and never finalizes.
    """

    def __init__(self, mode: str, *, reservation=None) -> None:
        self.mode = mode
        self.calls = 0
        self.requests: list[AgentRequest] = []
        self._reservation = reservation or _reservation()
        self._descriptor = AgentDescriptor(
            agent_id="phase11_stopping_agent",
            adapter_id="phase11_stopping_agent_adapter",
            mode=AdapterMode.REAL,
            operation_version="v1",
        )

    @property
    def descriptor(self):
        return self._descriptor

    @property
    def provider_call_reservation(self):
        return self._reservation

    @property
    def provider_admission_profile(self):
        return ProviderAdmissionProfile.ACCUMULATED_REMAINING_V1

    @staticmethod
    def _authoritative_browser_present(request: AgentRequest) -> bool:
        for observation in request.observations:
            result = observation.result
            if result.status is ToolInvocationStatus.SUCCEEDED:
                output = result.output
                if isinstance(output, BrowserResult):
                    if output.content_hash != sha256_text(output.content):
                        continue
                    if _AUTHORITATIVE_CONTENT in output.content:
                        return True
        return False

    @staticmethod
    def _browser_decision(url: str) -> Phase11RealWebAgentToolDecision:
        return Phase11RealWebAgentToolDecision(
            tool_call=RealWebAgentToolCall(
                tool_call_id=f"browse_{len(url)}",
                capability_id="web_browser",
                input=BrowserRequest(url=url),
            ),
            evidence_status="insufficient",
            remaining_evidence_gap=EvidenceGapCategory.AUTHORITATIVE_SOURCE_MISSING,
            usage=RuntimeResourceAmount(),
            usage_certainty=UsageCertainty.EXACT,
        )

    @staticmethod
    def _final_decision() -> AgentFinalDecision:
        return AgentFinalDecision(
            final=AgentFinalResult(outputs=()),
            usage=RuntimeResourceAmount(),
            usage_certainty=UsageCertainty.EXACT,
        )

    async def decide(self, request: AgentRequest, cancellation):
        del cancellation
        self.calls += 1
        self.requests.append(request)
        if self.mode == "case_c":
            return self._browser_decision(_AUTHORITATIVE_URL)
        if self._authoritative_browser_present(request):
            return self._final_decision()
        browser_calls = sum(
            1 for obs in request.observations if obs.capability_id == "web_browser"
        )
        if self.mode == "case_a":
            return self._browser_decision(_AUTHORITATIVE_URL)
        if self.mode == "case_b":
            return self._browser_decision(
                _INSUFFICIENT_URL if browser_calls == 0 else _AUTHORITATIVE_URL
            )
        raise AssertionError(f"unexpected mode {self.mode!r}")


def _context(*, tool_calls: int) -> AgentContext:
    task = ResearchTask(
        task_id="task_browse_authority",
        perspective_id="perspective_one",
        objective="Determine the authoritative fact from the source document.",
        expected_outputs=(),
        required_capability_ids=("web_browser",),
    )
    return AgentContext(
        run_id="run_phase11_stopping",
        run_revision=1,
        dag_id="dag_one",
        task=task,
        task_id=task.task_id,
        attempt_id="attempt_1",
        attempt_number=1,
        task_operation_key="a" * 64,
        task_attempt_key="b" * 64,
        task_idempotency=IdempotencyMode.IDEMPOTENT,
        deadline=Clock.current + timedelta(minutes=5),
        hard_limits=RuntimeResourceAmount(
            duration_milliseconds=300_000,
            tokens=100_000,
            cost_microunits=1_000_000,
            tool_calls=tool_calls,
        ),
        expected_outputs=(),
        authorized_capability_ids=("web_browser",),
    )


def _run(agent: StoppingAgent, tool: BrowserFakeTool, context: AgentContext):
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.REAL}))
    registry.register(tool)
    return asyncio.run(
        AgentRunner(
            agent=agent,
            registry=registry,
            policy=AgentRunnerPolicy(max_agent_steps=5, max_tool_calls=3),
            clock=Clock(),
            sleeper=Sleeper(),
            trace_sink=InMemoryTraceSink(),
        ).run(context, Signal())
    )


def test_case_a_authoritative_browser_result_is_final_next_decision() -> None:
    """Sufficient BrowserResult -> FINAL; exactly one browser call (no repeats)."""
    agent = StoppingAgent("case_a")
    tool = BrowserFakeTool()
    context = _context(tool_calls=3)
    result = _run(agent, tool, context)
    assert result.status.value == "succeeded"
    assert len(tool.calls) == 1
    assert agent.calls == 2
    assert [len(req.observations) for req in agent.requests] == [0, 1]
    # The final decision is reached with no further research tool call.
    assert result.final is not None
    assert all(obs.capability_id == "web_browser" for obs in result.observations)


def test_case_b_insufficient_browser_allows_one_more_then_final() -> None:
    """Insufficient BrowserResult with a bounded gap -> one more browser -> FINAL."""
    agent = StoppingAgent("case_b")
    tool = BrowserFakeTool()
    context = _context(tool_calls=3)
    result = _run(agent, tool, context)
    assert result.status.value == "succeeded"
    assert len(tool.calls) == 2
    assert agent.calls == 3
    # First browser result was insufficient; the second was authoritative.
    assert tool.calls[0].invocation.input.url == _INSUFFICIENT_URL
    assert tool.calls[1].invocation.input.url == _AUTHORITATIVE_URL


def test_case_c_pathological_repeats_fail_closed_on_hard_tool_limit() -> None:
    """Pathological repeated browsing still ends at the hard tool-call guard."""
    agent = StoppingAgent("case_c")
    tool = BrowserFakeTool()
    context = _context(tool_calls=3)
    result = _run(agent, tool, context)
    assert result.status.value == "failed"
    assert result.error is not None
    assert result.error.code == "agent_tool_call_limit_exceeded"
    assert len(tool.calls) == 3
    # The safety guard fired without raising max_agent_tool_calls or steps.
    assert agent.calls == 4
