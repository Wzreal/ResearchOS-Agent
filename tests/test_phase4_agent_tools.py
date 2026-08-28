from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FrozenClock, SequentialIds
from pydantic import ValidationError

from researchos.adapters.artifact_filesystem import FilesystemArtifactStore
from researchos.adapters.artifact_memory import InMemoryArtifactStore
from researchos.adapters.local_retrieval import (
    LocalRetrievalPolicy,
    LocalRetrievalTool,
)
from researchos.adapters.memory import InMemoryTraceSink
from researchos.adapters.mock_agent import (
    AgentFixture,
    AgentFixtureAction,
    AgentFixtureKey,
    ScriptedAgent,
)
from researchos.adapters.mock_tools import MockTool, ToolFixture, ToolFixtureKey
from researchos.adapters.python_subprocess import (
    PythonSubprocessPolicy,
    PythonSubprocessTool,
    _drain_stream,
)
from researchos.adapters.sleeper import AsyncioRunCancellationController
from researchos.application.agent_runner import AgentRunner, AgentRunnerPolicy
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.errors import (
    AgentContractError,
    ArtifactConflictError,
    CapabilityConfigurationError,
    ToolPermissionDenied,
    UnknownCapabilityError,
)
from researchos.domain.agent import (
    AgentContext,
    AgentDescriptor,
    AgentError,
    AgentFailedDecision,
    AgentFinalDecision,
    AgentFinalResult,
    AgentProducedOutput,
    AgentToolCall,
    AgentToolDecision,
)
from researchos.domain.contracts import TraceEventType, canonical_json_bytes
from researchos.domain.planning import ExpectedOutput, ResearchTask
from researchos.domain.runtime import (
    IdempotencyMode,
    RuntimeResourceAmount,
    UsageCertainty,
)
from researchos.domain.tools import (
    AdapterMode,
    BrowserRequest,
    BrowserResult,
    LocalRetrievalRequest,
    PythonExecutionRequest,
    SearchHit,
    SearchRequest,
    SearchResult,
    ToolArtifact,
    ToolDescriptor,
    ToolInvocationRequest,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


class NeverSleeper:
    async def sleep(self, milliseconds: int) -> None:
        del milliseconds
        await asyncio.Future()


class ImmediateSleeper:
    async def sleep(self, milliseconds: int) -> None:
        del milliseconds
        await asyncio.sleep(0)


class RecordingSearchTool:
    def __init__(
        self,
        *,
        idempotency: IdempotencyMode = IdempotencyMode.IDEMPOTENT,
        tokens: int = 0,
        usage: ToolUsage | None = None,
        certainty: UsageCertainty = UsageCertainty.EXACT,
        adapter_id: str = "mock_search",
        operation_version: str = "v1",
        capability_id: str = "search",
        tool_id: str = "search_tool",
    ) -> None:
        self.requests: list[ToolInvocationRequest] = []
        self._usage = usage or ToolUsage(tokens=tokens, tool_calls=1)
        self._certainty = certainty
        self._descriptor = ToolDescriptor(
            tool_id=tool_id,
            capability_id=capability_id,
            adapter_id=adapter_id,
            mode=AdapterMode.MOCK,
            operation_version=operation_version,
            input_type="search",
            output_type="search_result",
            side_effect=ToolSideEffect.NONE,
            idempotency=idempotency,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    async def invoke(self, request, cancellation):
        del cancellation
        self.requests.append(request)
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResult(adapter_id="mock_search", hits=()),
            usage=(
                None
                if self._certainty is UsageCertainty.UNKNOWN
                else self._usage
            ),
            usage_certainty=self._certainty,
        )


class HangingSearchTool(RecordingSearchTool):
    async def invoke(self, request, cancellation):
        del request, cancellation
        await asyncio.Future()


class FixedResultSearchTool(RecordingSearchTool):
    def __init__(self, result: object) -> None:
        super().__init__()
        self._result = result

    async def invoke(self, request, cancellation):
        del cancellation
        self.requests.append(request)
        return self._result


class PayloadSearchTool(RecordingSearchTool):
    async def invoke(self, request, cancellation):
        del cancellation
        self.requests.append(request)
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResult(
                adapter_id="mock_search",
                hits=(
                    SearchHit(
                        locator="result_one",
                        url="https://example.com/result",
                        title="raw-output-marker",
                        snippet="Bearer abcdefgh-secret-output",
                        retrieved_at=datetime.now(UTC),
                        adapter_id="mock_search",
                        provenance={
                            "source": "secret-source-marker",
                            "environment": "secret-env-marker",
                        },
                    ),
                ),
            ),
            usage=ToolUsage(tool_calls=1),
            usage_certainty=UsageCertainty.EXACT,
        )


class DelayedFinalAgent:
    def __init__(self) -> None:
        self._descriptor = AgentDescriptor(
            agent_id="delayed_agent",
            adapter_id="delayed",
            mode=AdapterMode.MOCK,
            operation_version="v1",
        )

    @property
    def descriptor(self):
        return self._descriptor

    async def decide(self, request, cancellation):
        del request, cancellation
        await asyncio.sleep(10)
        return AgentFinalDecision(
            final=AgentFinalResult(outputs=()),
            usage=_usage(),
            usage_certainty=UsageCertainty.EXACT,
        )


class RaisingAgent(DelayedFinalAgent):
    async def decide(self, request, cancellation):
        del request, cancellation
        raise RuntimeError("adapter failed")


class FailOnTerminalTrace(InMemoryTraceSink):
    def append(self, event):
        if event.event_type is TraceEventType.TOOL_INVOCATION_SUCCEEDED:
            raise OSError("fault")
        super().append(event)


def _task(*, capabilities=("search",)) -> ResearchTask:
    return ResearchTask(
        task_id="task_one",
        perspective_id="perspective_one",
        objective="Research the question",
        required_capability_ids=capabilities,
        expected_outputs=(
            ExpectedOutput(
                output_id="answer",
                description="answer",
                media_type="text/plain",
            ),
        ),
    )


def _context(
    clock: FrozenClock,
    *,
    attempt_number: int = 1,
    task_idempotency: IdempotencyMode = IdempotencyMode.IDEMPOTENT,
    limits: RuntimeResourceAmount | None = None,
    attempt_key: str = HASH_B,
) -> AgentContext:
    task = _task()
    return AgentContext(
        run_id="run_one",
        run_revision=4,
        dag_id="dag_one",
        task=task,
        task_id=task.task_id,
        attempt_id=f"attempt_{attempt_number}",
        attempt_number=attempt_number,
        task_operation_key=HASH_A,
        task_attempt_key=attempt_key,
        task_idempotency=task_idempotency,
        deadline=clock.now() + timedelta(seconds=30),
        hard_limits=limits
        or RuntimeResourceAmount(
            duration_milliseconds=30_000,
            tokens=100,
            cost_microunits=100,
            tool_calls=2,
        ),
        expected_outputs=task.expected_outputs,
        authorized_capability_ids=task.required_capability_ids,
    )


def _descriptor() -> AgentDescriptor:
    return AgentDescriptor(
        agent_id="scripted_agent",
        adapter_id="scripted",
        mode=AdapterMode.MOCK,
        operation_version="v1",
    )


def _usage() -> RuntimeResourceAmount:
    return RuntimeResourceAmount()


def _runner(
    agent,
    tool,
    clock,
    *,
    sleeper=None,
    trace=None,
    policy=None,
) -> AgentRunner:
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
    registry.register(tool)
    return AgentRunner(
        agent=agent,
        registry=registry,
        policy=policy or AgentRunnerPolicy(max_agent_steps=3, max_tool_calls=2),
        clock=clock,
        sleeper=sleeper or NeverSleeper(),
        trace_sink=trace or InMemoryTraceSink(),
        id_factory=SequentialIds(),
    )


def test_agent_never_returns_is_bounded_by_deadline() -> None:
    clock = FrozenClock()
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                action=AgentFixtureAction.NEVER_RETURN
            )
        },
    )
    result = asyncio.run(
        _runner(
            agent,
            RecordingSearchTool(),
            clock,
            sleeper=ImmediateSleeper(),
        ).run(_context(clock), AsyncioRunCancellationController().signal_for_attempt())
    )
    assert result.error is not None
    assert result.error.code == "agent_decision_timed_out"
    assert result.usage is None
    assert result.usage_certainty is UsageCertainty.UNKNOWN


def test_agent_direct_final_has_ordered_trace() -> None:
    clock = FrozenClock()
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentFinalDecision(
                    final=AgentFinalResult(
                        outputs=(
                            AgentProducedOutput(
                                output_id="answer", media_type="text/plain"
                            ),
                        )
                    ),
                    usage=RuntimeResourceAmount(tokens=3),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    trace = InMemoryTraceSink()
    result = asyncio.run(
        _runner(agent, RecordingSearchTool(), clock, trace=trace).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    events = trace.read("run_one")
    assert result.status.value == "succeeded"
    assert [event.event_type for event in events] == [
        TraceEventType.AGENT_STARTED,
        TraceEventType.AGENT_DECISION,
        TraceEventType.AGENT_COMPLETED,
    ]
    assert events[1].causation_id == events[0].event_id
    assert events[2].causation_id == events[1].event_id
    assert {event.correlation_id for event in events} == {"attempt_1"}


def test_agent_tool_observation_then_final_is_structured() -> None:
    clock = FrozenClock()
    trace = InMemoryTraceSink()
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query="raw-query-marker"),
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            ),
            AgentFixtureKey("task_one", 1, 2): AgentFixture(
                decision=AgentFinalDecision(
                    final=AgentFinalResult(
                        outputs=(
                            AgentProducedOutput(
                                output_id="answer", media_type="text/plain"
                            ),
                        )
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            ),
        },
    )
    result = asyncio.run(
        _runner(agent, RecordingSearchTool(), clock, trace=trace).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.status.value == "succeeded"
    observation = agent.requests[1].observations[0]
    assert observation.tool_call_id == "call_one"
    assert observation.result.status is ToolInvocationStatus.SUCCEEDED
    events = trace.read("run_one")
    event_types = [event.event_type for event in events]
    requested_index = event_types.index(TraceEventType.AGENT_TOOL_REQUESTED)
    started_index = event_types.index(TraceEventType.TOOL_INVOCATION_STARTED)
    terminal_index = event_types.index(TraceEventType.TOOL_INVOCATION_SUCCEEDED)
    assert requested_index < started_index < terminal_index
    assert events[started_index].causation_id == events[requested_index].event_id
    assert events[terminal_index].causation_id == events[started_index].event_id
    assert events[started_index].correlation_id == events[terminal_index].correlation_id
    persisted = "".join(event.model_dump_json() for event in events)
    assert "raw-query-marker" not in persisted
    assert "source" not in persisted
    assert "environment" not in persisted


def test_trace_excludes_raw_tool_output_provenance_and_secret() -> None:
    clock = FrozenClock()
    trace = InMemoryTraceSink()
    result = asyncio.run(
        _runner(
            _tool_then_final_agent(first_usage=_usage(), final_usage=_usage()),
            PayloadSearchTool(),
            clock,
            trace=trace,
        ).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.status.value == "succeeded"
    persisted = "".join(
        event.model_dump_json() for event in trace.read("run_one")
    )
    for forbidden in (
        "raw-output-marker",
        "abcdefgh-secret-output",
        "secret-source-marker",
        "secret-env-marker",
    ):
        assert forbidden not in persisted


def test_malformed_decision_fails_without_payload_leakage() -> None:
    clock = FrozenClock()
    secret = "Bearer abcdefgh-secret-value"
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision={"kind": "tool_call", "payload": secret}
            )
        },
    )
    trace = InMemoryTraceSink()
    result = asyncio.run(
        _runner(agent, RecordingSearchTool(), clock, trace=trace).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "agent_malformed_decision"
    persisted = "".join(
        event.model_dump_json() for event in trace.read("run_one")
    )
    assert secret not in persisted


def test_agent_failure_and_max_steps_are_distinct() -> None:
    clock = FrozenClock()
    failed = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentFailedDecision(
                    error=AgentError(
                        code="agent_declined", message="cannot complete"
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    failed_result = asyncio.run(
        _runner(failed, RecordingSearchTool(), clock).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert failed_result.error is not None
    assert failed_result.error.code == "agent_declined"

    looping = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query="query"),
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    maxed = asyncio.run(
        _runner(
            looping,
            RecordingSearchTool(),
            clock,
            policy=AgentRunnerPolicy(max_agent_steps=1, max_tool_calls=1),
        ).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert maxed.error is not None
    assert maxed.error.code == "agent_max_steps_exceeded"


def test_cancellation_interrupts_agent_decision() -> None:
    async def scenario():
        clock = FrozenClock()
        agent = ScriptedAgent(
            _descriptor(),
            {
                AgentFixtureKey("task_one", 1, 1): AgentFixture(
                    action=AgentFixtureAction.WAIT_FOR_CANCELLATION
                )
            },
        )
        controller = AsyncioRunCancellationController()
        running = asyncio.create_task(
            _runner(agent, RecordingSearchTool(), clock).run(
                _context(clock), controller.signal_for_attempt()
            )
        )
        await asyncio.sleep(0)
        controller.request_cancel()
        return await running

    result = asyncio.run(scenario())
    assert result.error is not None
    assert result.error.code == "agent_cancelled"
    assert result.usage is None
    assert result.usage_certainty is UsageCertainty.UNKNOWN


def test_deadline_interrupts_a_decision_that_would_eventually_return() -> None:
    clock = FrozenClock()
    result = asyncio.run(
        _runner(
            DelayedFinalAgent(),
            RecordingSearchTool(),
            clock,
            sleeper=ImmediateSleeper(),
        ).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "agent_decision_timed_out"
    assert result.usage is None
    assert result.usage_certainty is UsageCertainty.UNKNOWN


def test_agent_adapter_exception_after_dispatch_has_unknown_usage() -> None:
    clock = FrozenClock()
    result = asyncio.run(
        _runner(RaisingAgent(), RecordingSearchTool(), clock).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "agent_decision_failed"
    assert result.usage is None
    assert result.usage_certainty is UsageCertainty.UNKNOWN


def test_tool_operation_key_ignores_attempt_and_tool_call_identity() -> None:
    clock = FrozenClock()
    tool = RecordingSearchTool()
    fixtures = {}
    for attempt, call_id in ((1, "call_one"), (2, "call_two")):
        fixtures[AgentFixtureKey("task_one", attempt, 1)] = AgentFixture(
            decision=AgentToolDecision(
                tool_call=AgentToolCall(
                    tool_call_id=call_id,
                    capability_id="search",
                    input=SearchRequest(query="same normalized input"),
                ),
                usage=_usage(),
                usage_certainty=UsageCertainty.EXACT,
            )
        )
        fixtures[AgentFixtureKey("task_one", attempt, 2)] = AgentFixture(
            decision=AgentFinalDecision(
                final=AgentFinalResult(
                    outputs=(
                        AgentProducedOutput(
                            output_id="answer", media_type="text/plain"
                        ),
                    )
                ),
                usage=_usage(),
                usage_certainty=UsageCertainty.EXACT,
            )
        )
    runner = _runner(ScriptedAgent(_descriptor(), fixtures), tool, clock)
    for attempt in (1, 2):
        result = asyncio.run(
            runner.run(
                _context(
                    clock,
                    attempt_number=attempt,
                    attempt_key=HASH_B if attempt == 1 else HASH_C,
                ),
                AsyncioRunCancellationController().signal_for_attempt(),
            )
        )
        assert result.status.value == "succeeded"
    assert tool.requests[0].tool_call_id != tool.requests[1].tool_call_id
    assert tool.requests[0].attempt_id != tool.requests[1].attempt_id
    assert tool.requests[0].tool_operation_key == tool.requests[1].tool_operation_key


def _capture_operation_key(
    *,
    query: str = "query",
    adapter_id: str = "mock_search",
    operation_version: str = "v1",
) -> str:
    clock = FrozenClock()
    tool = RecordingSearchTool(
        adapter_id=adapter_id, operation_version=operation_version
    )
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query=query),
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    asyncio.run(
        _runner(
            agent,
            tool,
            clock,
            policy=AgentRunnerPolicy(max_agent_steps=1, max_tool_calls=1),
        ).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    return tool.requests[0].tool_operation_key


def test_tool_operation_key_changes_with_input_version_and_adapter() -> None:
    baseline = _capture_operation_key()
    assert _capture_operation_key(query="different") != baseline
    assert _capture_operation_key(operation_version="v2") != baseline
    assert _capture_operation_key(adapter_id="other_adapter") != baseline


@pytest.mark.parametrize(
    ("tool_result", "expected_code"),
    [
        (
            ToolInvocationResult(
                status=ToolInvocationStatus.SUCCEEDED,
                output=BrowserResult(
                    adapter_id="mock_search",
                    final_url="https://example.com",
                    title="title",
                    content="content",
                    content_hash=HASH_A,
                    retrieved_at=datetime.now(UTC),
                ),
                usage=ToolUsage(),
                usage_certainty=UsageCertainty.EXACT,
            ),
            "tool_output_contract_violation",
        ),
        (
            ToolInvocationResult(
                status=ToolInvocationStatus.SUCCEEDED,
                output=SearchResult(adapter_id="other_adapter", hits=()),
                usage=ToolUsage(),
                usage_certainty=UsageCertainty.EXACT,
            ),
            "tool_provenance_mismatch",
        ),
        *[
            (
                ToolInvocationResult(
                    status=ToolInvocationStatus.SUCCEEDED,
                    output=SearchResult(adapter_id="mock_search", hits=()),
                    artifacts=(
                        ToolArtifact(
                            artifact_id="artifact_one",
                            media_type="text/plain",
                            relative_path="tools/value.txt",
                            sha256=HASH_A,
                            size_bytes=1,
                            producer_tool_id=producer,
                            tool_operation_key=operation_key,
                        ),
                    ),
                    usage=ToolUsage(),
                    usage_certainty=UsageCertainty.EXACT,
                ),
                "tool_provenance_mismatch",
            )
            for producer, operation_key in (
                ("other_tool", HASH_A),
                ("search_tool", HASH_B),
            )
        ],
    ],
)
def test_tool_result_integrity_is_checked_before_observation(
    tool_result: ToolInvocationResult, expected_code: str
) -> None:
    clock = FrozenClock()
    agent = _tool_then_final_agent(first_usage=_usage(), final_usage=_usage())
    result = asyncio.run(
        _runner(agent, FixedResultSearchTool(tool_result), clock).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == expected_code
    assert result.error.retryable
    assert len(agent.requests) == 1


def test_unvalidated_tool_result_is_not_observed() -> None:
    clock = FrozenClock()
    agent = _tool_then_final_agent(first_usage=_usage(), final_usage=_usage())
    result = asyncio.run(
        _runner(agent, FixedResultSearchTool({"raw": "result"}), clock).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "tool_output_contract_violation"
    assert result.usage is None
    assert result.usage_certainty is UsageCertainty.UNKNOWN
    assert len(agent.requests) == 1


def test_observation_byte_limit_has_exact_boundary() -> None:
    tool_result = ToolInvocationResult(
        status=ToolInvocationStatus.SUCCEEDED,
        output=SearchResult(adapter_id="mock_search", hits=()),
        usage=ToolUsage(),
        usage_certainty=UsageCertainty.EXACT,
    )
    size = len(canonical_json_bytes(tool_result))
    for limit, succeeds in ((size, True), (size - 1, False)):
        clock = FrozenClock()
        result = asyncio.run(
            _runner(
                _tool_then_final_agent(first_usage=_usage(), final_usage=_usage()),
                FixedResultSearchTool(tool_result),
                clock,
                policy=AgentRunnerPolicy(
                    max_agent_steps=2,
                    max_tool_calls=1,
                    max_observation_bytes=limit,
                ),
            ).run(
                _context(clock),
                AsyncioRunCancellationController().signal_for_attempt(),
            )
        )
        if succeeds:
            assert result.status.value == "succeeded"
        else:
            assert result.error is not None
            assert result.error.code == "tool_result_too_large"


def test_registry_presence_does_not_grant_permission() -> None:
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
    registry.register(RecordingSearchTool())
    with pytest.raises(ToolPermissionDenied):
        registry.resolve_authorized("search", ())


def test_registry_rejects_duplicate_and_unknown_entries() -> None:
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
    first = RecordingSearchTool()
    registry.register(first)
    with pytest.raises(CapabilityConfigurationError):
        registry.register(RecordingSearchTool(adapter_id="other_adapter"))
    duplicate_tool = RecordingSearchTool(
        capability_id="other_capability", tool_id="search_tool"
    )
    with pytest.raises(CapabilityConfigurationError):
        registry.register(duplicate_tool)
    with pytest.raises(UnknownCapabilityError, match="missing"):
        registry.resolve("missing")


def test_registry_rejects_mode_without_fallback() -> None:
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.LOCAL}))
    with pytest.raises(CapabilityConfigurationError):
        registry.register(RecordingSearchTool())


def test_runner_rejects_agent_mode_without_fallback() -> None:
    descriptor = _descriptor().model_copy(update={"mode": AdapterMode.REAL})
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
    registry.register(RecordingSearchTool())
    with pytest.raises(CapabilityConfigurationError):
        AgentRunner(
            agent=type(
                "RealAgent",
                (),
                {"descriptor": descriptor},
            )(),
            registry=registry,
            policy=AgentRunnerPolicy(max_agent_steps=1, max_tool_calls=0),
            clock=FrozenClock(),
            sleeper=NeverSleeper(),
            trace_sink=InMemoryTraceSink(),
        )


def test_mock_adapters_cannot_claim_real_mode() -> None:
    real_agent = _descriptor().model_copy(update={"mode": AdapterMode.REAL})
    with pytest.raises(AgentContractError, match="mock adapter mode"):
        ScriptedAgent(real_agent, {})
    real_tool = RecordingSearchTool().descriptor.model_copy(
        update={"mode": AdapterMode.REAL}
    )
    with pytest.raises(AgentContractError, match="mock adapter mode"):
        MockTool(real_tool, {})


def test_known_tool_overrun_is_not_clamped() -> None:
    clock = FrozenClock()
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query="query"),
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    limits = RuntimeResourceAmount(
        duration_milliseconds=30_000,
        tokens=5,
        cost_microunits=100,
        tool_calls=1,
    )
    result = asyncio.run(
        _runner(agent, RecordingSearchTool(tokens=9), clock).run(
            _context(clock, limits=limits),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "agent_hard_limit_exceeded"
    assert result.usage is not None and result.usage.tokens == 9


def _tool_then_final_agent(
    *,
    first_usage: RuntimeResourceAmount | None = None,
    first_certainty: UsageCertainty = UsageCertainty.EXACT,
    final_usage: RuntimeResourceAmount | None = None,
    final_certainty: UsageCertainty = UsageCertainty.EXACT,
) -> ScriptedAgent:
    return ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query="query"),
                    ),
                    usage=first_usage,
                    usage_certainty=first_certainty,
                )
            ),
            AgentFixtureKey("task_one", 1, 2): AgentFixture(
                decision=AgentFinalDecision(
                    final=AgentFinalResult(
                        outputs=(
                            AgentProducedOutput(
                                output_id="answer", media_type="text/plain"
                            ),
                        )
                    ),
                    usage=final_usage,
                    usage_certainty=final_certainty,
                )
            ),
        },
    )


def test_exact_and_upper_bound_usage_aggregate_without_second_ledger() -> None:
    clock = FrozenClock()
    agent = _tool_then_final_agent(
        first_usage=RuntimeResourceAmount(
            duration_milliseconds=2, tokens=3, cost_microunits=4
        ),
        final_usage=RuntimeResourceAmount(
            duration_milliseconds=5, tokens=6, cost_microunits=7
        ),
    )
    tool = RecordingSearchTool(
        usage=ToolUsage(
            duration_milliseconds=11,
            tokens=13,
            cost_microunits=17,
            tool_calls=1,
        ),
        certainty=UsageCertainty.UPPER_BOUND,
    )
    result = asyncio.run(
        _runner(agent, tool, clock).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.usage_certainty is UsageCertainty.UPPER_BOUND
    assert result.usage == RuntimeResourceAmount(
        duration_milliseconds=18,
        tokens=22,
        cost_microunits=28,
        tool_calls=1,
    )


def test_unknown_tool_usage_propagates_without_amount() -> None:
    clock = FrozenClock()
    result = asyncio.run(
        _runner(
            _tool_then_final_agent(
                first_usage=_usage(), final_usage=_usage()
            ),
            RecordingSearchTool(certainty=UsageCertainty.UNKNOWN),
            clock,
        ).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None and result.error.code == "tool_usage_unknown"
    assert result.usage is None
    assert result.usage_certainty is UsageCertainty.UNKNOWN


@pytest.mark.parametrize(
    ("usage", "limits"),
    [
        (
            ToolUsage(duration_milliseconds=9, tool_calls=1),
            RuntimeResourceAmount(
                duration_milliseconds=8,
                tokens=100,
                cost_microunits=100,
                tool_calls=2,
            ),
        ),
        (
            ToolUsage(tokens=9, tool_calls=1),
            RuntimeResourceAmount(
                duration_milliseconds=100,
                tokens=8,
                cost_microunits=100,
                tool_calls=2,
            ),
        ),
        (
            ToolUsage(cost_microunits=9, tool_calls=1),
            RuntimeResourceAmount(
                duration_milliseconds=100,
                tokens=100,
                cost_microunits=8,
                tool_calls=2,
            ),
        ),
        (
            ToolUsage(tool_calls=2),
            RuntimeResourceAmount(
                duration_milliseconds=100,
                tokens=100,
                cost_microunits=100,
                tool_calls=1,
            ),
        ),
    ],
)
def test_each_hard_limit_preserves_known_overrun(usage, limits) -> None:
    clock = FrozenClock()
    result = asyncio.run(
        _runner(
            _tool_then_final_agent(first_usage=_usage(), final_usage=_usage()),
            RecordingSearchTool(usage=usage),
            clock,
        ).run(
            _context(clock, limits=limits),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "agent_hard_limit_exceeded"
    assert result.usage is not None
    assert result.usage.tool_calls == usage.tool_calls


def test_tool_never_returns_is_bounded_and_usage_becomes_unknown() -> None:
    clock = FrozenClock()
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query="query"),
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    trace = InMemoryTraceSink()
    result = asyncio.run(
        _runner(
            agent,
            HangingSearchTool(),
            clock,
            sleeper=ImmediateSleeper(),
            trace=trace,
        ).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None and result.error.code == "tool_timed_out"
    assert result.usage is None
    assert result.usage_certainty is UsageCertainty.UNKNOWN
    assert TraceEventType.TOOL_INVOCATION_TIMED_OUT in {
        event.event_type for event in trace.read("run_one")
    }


@pytest.mark.parametrize(
    ("task_mode", "tool_mode", "expected"),
    [
        (IdempotencyMode.IDEMPOTENT, IdempotencyMode.IDEMPOTENT, True),
        (IdempotencyMode.NON_IDEMPOTENT, IdempotencyMode.IDEMPOTENT, False),
        (IdempotencyMode.NON_IDEMPOTENT, IdempotencyMode.NON_IDEMPOTENT, False),
    ],
)
def test_terminal_trace_retry_requires_both_idempotent_layers(
    task_mode, tool_mode, expected
) -> None:
    clock = FrozenClock()
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_one", 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query="query"),
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    result = asyncio.run(
        _runner(
            agent,
            RecordingSearchTool(idempotency=tool_mode),
            clock,
            trace=FailOnTerminalTrace(),
        ).run(
            _context(clock, task_idempotency=task_mode),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "tool_trace_terminal_failed"
    assert result.error.retryable is expected
    assert result.usage is not None and result.usage.tool_calls == 1


def test_terminal_trace_retry_respects_failure_policy() -> None:
    clock = FrozenClock()
    agent = _tool_then_final_agent(first_usage=_usage(), final_usage=_usage())
    result = asyncio.run(
        _runner(
            agent,
            RecordingSearchTool(),
            clock,
            trace=FailOnTerminalTrace(),
            policy=AgentRunnerPolicy(
                max_agent_steps=2,
                max_tool_calls=1,
                trace_failure_retryable=False,
            ),
        ).run(
            _context(clock),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "tool_trace_terminal_failed"
    assert not result.error.retryable


def _invocation(payload) -> ToolInvocationRequest:
    return ToolInvocationRequest(
        invocation_id="invocation_one",
        run_id="run_one",
        task_id="task_one",
        attempt_id="attempt_one",
        agent_step=1,
        tool_call_id="call_one",
        tool_operation_key=HASH_A,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        input=payload,
    )


def test_python_subprocess_publishes_declared_artifact() -> None:
    store = InMemoryArtifactStore()
    tool = PythonSubprocessTool(artifact_store=store)
    result = asyncio.run(
        tool.invoke(
            _invocation(
                PythonExecutionRequest(
                    source=(
                        "from pathlib import Path\n"
                        "Path('answer.txt').write_text('42', encoding='utf-8')\n"
                        "print(42)\n"
                    ),
                    artifact_paths=("answer.txt",),
                )
            ),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.status is ToolInvocationStatus.FAILED
    assert result.error is not None
    assert result.error.code == "unsupported_python_code"

    result = asyncio.run(
        tool.invoke(
            _invocation(
                PythonExecutionRequest(
                    source="open('answer.txt', 'w', encoding='utf-8').write('42')\n",
                    artifact_paths=("answer.txt",),
                )
            ),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.artifacts[0].relative_path.endswith("answer.txt")
    assert store.read("run_one", result.artifacts[0].relative_path) == b"42"


def test_filesystem_artifact_store_is_idempotent_and_detects_conflict(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    first = store.store_bytes(
        run_id="run_one",
        relative_path="tools/output.txt",
        content=b"first",
        media_type="text/plain",
        producer_tool_id="python",
        tool_operation_key=HASH_A,
    )
    repeated = store.store_bytes(
        run_id="run_one",
        relative_path="tools/output.txt",
        content=b"first",
        media_type="text/plain",
        producer_tool_id="python",
        tool_operation_key=HASH_A,
    )
    assert repeated == first
    with pytest.raises(ArtifactConflictError):
        store.store_bytes(
            run_id="run_one",
            relative_path="tools/output.txt",
            content=b"different",
            media_type="text/plain",
            producer_tool_id="python",
            tool_operation_key=HASH_A,
        )


def test_filesystem_artifact_store_rejects_traversal_and_symlink_escape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "root")
    with pytest.raises(ValidationError):
        store.store_bytes(
            run_id="run_one",
            relative_path="../escape.txt",
            content=b"no",
            media_type="text/plain",
            producer_tool_id="python",
            tool_operation_key=HASH_A,
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    run_root = tmp_path / "root" / "run_one"
    run_root.mkdir(parents=True)
    link = run_root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == link / "escape.txt":
                return outside / "escape.txt"
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ArtifactConflictError):
        store.store_bytes(
            run_id="run_one",
            relative_path="link/escape.txt",
            content=b"no",
            media_type="text/plain",
            producer_tool_id="python",
            tool_operation_key=HASH_A,
        )


def test_local_retrieval_is_deterministic_and_rejects_malformed(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        json.dumps(
            {
                "document_id": "doc_one",
                "chunk_id": "chunk_one",
                "locator": "local:one",
                "content": "alpha beta alpha",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tool = LocalRetrievalTool(
        root=tmp_path,
        corpus_path="corpus.jsonl",
        clock=FrozenClock(),
    )
    result = asyncio.run(
        tool.invoke(
            _invocation(LocalRetrievalRequest(query="alpha")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output is not None
    assert result.output.hits[0].chunk_id == "chunk_one"

    corpus.write_text("{not-json}\n", encoding="utf-8")
    malformed = asyncio.run(
        tool.invoke(
            _invocation(LocalRetrievalRequest(query="alpha")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert malformed.error is not None
    assert malformed.error.code == "malformed_corpus"


def test_local_retrieval_tie_break_empty_duplicate_and_content_hash(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "document_id": "doc_b",
            "chunk_id": "chunk_b",
            "locator": "local:b",
            "content": "alpha",
            "source_metadata": {"source": "b"},
        },
        {
            "document_id": "doc_a",
            "chunk_id": "chunk_a",
            "locator": "local:a",
            "content": "alpha",
            "source_metadata": {"source": "a"},
        },
    ]
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    tool = LocalRetrievalTool(
        root=tmp_path, corpus_path="corpus.jsonl", clock=FrozenClock()
    )
    first = asyncio.run(
        tool.invoke(
            _invocation(LocalRetrievalRequest(query="alpha")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    second = asyncio.run(
        tool.invoke(
            _invocation(LocalRetrievalRequest(query="alpha")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert first == second
    assert first.output is not None
    assert [hit.document_id for hit in first.output.hits] == ["doc_a", "doc_b"]
    assert first.output.hits[0].source_metadata == {"source": "a"}
    assert first.output.hits[0].content_hash == hashlib.sha256(b"alpha").hexdigest()

    empty = asyncio.run(
        tool.invoke(
            _invocation(LocalRetrievalRequest(query="unmatched")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert empty.output is not None and empty.output.hits == ()
    assert empty.output.matched_count == 0

    corpus.write_text(
        json.dumps(rows[0]) + "\n" + json.dumps(rows[0]) + "\n",
        encoding="utf-8",
    )
    duplicate = asyncio.run(
        tool.invoke(
            _invocation(LocalRetrievalRequest(query="alpha")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert duplicate.error is not None
    assert duplicate.error.code == "malformed_corpus"


def test_local_retrieval_rejects_traversal_and_symlink_escape(
    tmp_path: Path, monkeypatch
) -> None:
    with pytest.raises(ValueError, match="safe relative"):
        LocalRetrievalTool(
            root=tmp_path, corpus_path="../outside.jsonl", clock=FrozenClock()
        )
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "corpus.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == link:
                return outside
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="escapes"):
        LocalRetrievalTool(
            root=root, corpus_path="corpus.jsonl", clock=FrozenClock()
        )


def test_local_retrieval_enforces_corpus_byte_and_chunk_limits(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus.jsonl"
    row = json.dumps(
        {
            "document_id": "doc_one",
            "chunk_id": "chunk_one",
            "locator": "local:one",
            "content": "alpha",
        }
    )
    corpus.write_text(row + "\n", encoding="utf-8")
    byte_limited = LocalRetrievalTool(
        root=tmp_path,
        corpus_path="corpus.jsonl",
        clock=FrozenClock(),
        policy=LocalRetrievalPolicy(max_corpus_bytes=1),
    )
    result = asyncio.run(
        byte_limited.invoke(
            _invocation(LocalRetrievalRequest(query="alpha")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "corpus_limit_exceeded"

    corpus.write_text(row + "\n" + row.replace("chunk_one", "chunk_two") + "\n")
    chunk_limited = LocalRetrievalTool(
        root=tmp_path,
        corpus_path="corpus.jsonl",
        clock=FrozenClock(),
        policy=LocalRetrievalPolicy(max_chunk_count=1),
    )
    result = asyncio.run(
        chunk_limited.invoke(
            _invocation(LocalRetrievalRequest(query="alpha")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "corpus_limit_exceeded"


def test_local_retrieval_worker_does_not_block_event_loop(tmp_path: Path) -> None:
    (tmp_path / "corpus.jsonl").write_text("", encoding="utf-8")
    tool = LocalRetrievalTool(
        root=tmp_path, corpus_path="corpus.jsonl", clock=FrozenClock()
    )
    started = threading.Event()
    release = threading.Event()
    original = tool._retrieve

    def slow(payload):
        started.set()
        release.wait()
        return original(payload)

    tool._retrieve = slow

    async def scenario():
        running = asyncio.create_task(
            tool.invoke(
                _invocation(LocalRetrievalRequest(query="alpha")),
                AsyncioRunCancellationController().signal_for_attempt(),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        marker = False

        async def tick():
            nonlocal marker
            await asyncio.sleep(0)
            marker = True

        await asyncio.wait_for(tick(), 0.1)
        release.set()
        await running
        return marker

    assert asyncio.run(scenario())


@pytest.mark.parametrize("cause", ["cancelled", "timed_out"])
def test_local_retrieval_worker_is_bounded_by_outer_signal(
    tmp_path: Path, cause: str
) -> None:
    (tmp_path / "corpus.jsonl").write_text("", encoding="utf-8")
    clock = FrozenClock()
    tool = LocalRetrievalTool(
        root=tmp_path, corpus_path="corpus.jsonl", clock=clock
    )
    started = threading.Event()
    release = threading.Event()
    original = tool._retrieve

    def slow(payload):
        started.set()
        release.wait()
        return original(payload)

    tool._retrieve = slow

    async def scenario():
        controller = AsyncioRunCancellationController()
        invocation = _invocation(LocalRetrievalRequest(query="alpha"))
        if cause == "timed_out":
            invocation = invocation.model_copy(
                update={"deadline": clock.now() + timedelta(milliseconds=10)}
            )
        running = asyncio.create_task(
            tool.invoke(invocation, controller.signal_for_attempt())
        )
        while not started.is_set():
            await asyncio.sleep(0)
        if cause == "cancelled":
            controller.request_cancel()
        result = await running
        release.set()
        return result

    result = asyncio.run(scenario())
    assert result.status.value == cause
    assert result.usage is None
    assert result.usage_certainty is UsageCertainty.UNKNOWN


def test_python_syntax_runtime_and_output_limits() -> None:
    tool = PythonSubprocessTool(artifact_store=InMemoryArtifactStore())
    syntax = asyncio.run(
        tool.invoke(
            _invocation(PythonExecutionRequest(source="if:")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert syntax.error is not None
    assert syntax.error.code == "unsupported_python_code"
    runtime = asyncio.run(
        tool.invoke(
            _invocation(PythonExecutionRequest(source="raise RuntimeError('x')")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert runtime.error is not None
    assert runtime.error.code == "python_process_failed"
    bounded_tool = PythonSubprocessTool(
        artifact_store=InMemoryArtifactStore(),
        policy=PythonSubprocessPolicy(max_output_bytes=4),
    )
    for source in (
        "print('abcdef')",
        "print('uvwxyz', file=__import__('sys').stderr)",
    ):
        bounded = asyncio.run(
            bounded_tool.invoke(
                _invocation(PythonExecutionRequest(source=source)),
                AsyncioRunCancellationController().signal_for_attempt(),
            )
        )
        assert bounded.output is None
        assert bounded.error is not None
        assert bounded.error.code == "python_output_limit_exceeded"
        assert bounded.usage_certainty is UsageCertainty.UNKNOWN


def test_python_stream_drain_retains_at_most_the_hard_limit() -> None:
    class Stream:
        def __init__(self) -> None:
            self.calls = 0

        async def read(self, limit):
            del limit
            self.calls += 1
            return b"x" * 1_000_000

    stream = Stream()
    result = asyncio.run(_drain_stream(stream, 4))
    assert result.exceeded
    assert result.value == b"xxxx"
    assert stream.calls == 1


def test_python_output_limit_terminates_and_reaps_child(monkeypatch) -> None:
    class Stream:
        def __init__(self, process, value):
            self.process = process
            self.value = value

        async def read(self, limit):
            del limit
            if self.value:
                value, self.value = self.value, b""
                return value
            while self.process.returncode is None:
                await asyncio.sleep(0)
            return b""

    class Process:
        returncode = None
        terminated = False
        reaped = False

        def __init__(self):
            self.stdout = Stream(self, b"too much output")
            self.stderr = Stream(self, b"")

        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0)
            self.reaped = True
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -1

        def kill(self):
            self.returncode = -9

    process = Process()

    async def create(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    result = asyncio.run(
        PythonSubprocessTool(
            artifact_store=InMemoryArtifactStore(),
            policy=PythonSubprocessPolicy(max_output_bytes=4),
        ).invoke(
            _invocation(PythonExecutionRequest(source="print('value')")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.error is not None
    assert result.error.code == "python_output_limit_exceeded"
    assert process.terminated
    assert process.reaped


def test_python_artifact_count_size_and_path_limits() -> None:
    count_tool = PythonSubprocessTool(
        artifact_store=InMemoryArtifactStore(),
        policy=PythonSubprocessPolicy(max_artifact_count=1),
    )
    too_many = asyncio.run(
        count_tool.invoke(
            _invocation(
                PythonExecutionRequest(
                    source="open('a', 'w').write('a'); open('b', 'w').write('b')",
                    artifact_paths=("a", "b"),
                )
            ),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert too_many.error is not None
    assert too_many.error.code == "python_artifact_failed"
    size_tool = PythonSubprocessTool(
        artifact_store=InMemoryArtifactStore(),
        policy=PythonSubprocessPolicy(max_artifact_bytes=1),
    )
    too_large = asyncio.run(
        size_tool.invoke(
            _invocation(
                PythonExecutionRequest(
                    source="open('a', 'w').write('ab')", artifact_paths=("a",)
                )
            ),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert too_large.error is not None
    assert too_large.error.code == "python_artifact_failed"
    with pytest.raises(ValidationError):
        PythonExecutionRequest(source="print(1)", artifact_paths=("../a",))


def test_python_environment_stdin_and_exec_shape_are_restricted(monkeypatch) -> None:
    captured = {}

    class Process:
        returncode = 0

        class Stream:
            def __init__(self, value):
                self.value = value

            async def read(self, limit):
                del limit
                value, self.value = self.value, b""
                return value

        stdout = Stream(b"ok")
        stderr = Stream(b"")

        async def wait(self):
            return self.returncode

    async def create(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setenv("RESEARCHOS_TEST_SECRET", "must-not-leak")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    tool = PythonSubprocessTool(artifact_store=InMemoryArtifactStore())
    result = asyncio.run(
        tool.invoke(
            _invocation(PythonExecutionRequest(source="print('ok')")),
            AsyncioRunCancellationController().signal_for_attempt(),
        )
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert captured["args"][1:] == ("-I", "program.py")
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL
    assert "RESEARCHOS_TEST_SECRET" not in captured["kwargs"]["env"]


def test_python_cancellation_terminates_child(monkeypatch) -> None:
    started = asyncio.Event()

    class Process:
        returncode = None
        terminated = False
        killed = False

        class Stream:
            def __init__(self, process):
                self.process = process

            async def read(self, limit):
                del limit
                while self.process.returncode is None:
                    await asyncio.sleep(0)
                return b""

        def __init__(self):
            self.stdout = self.Stream(self)
            self.stderr = self.Stream(self)

        async def wait(self):
            started.set()
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -1

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = Process()

    async def create(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    async def scenario():
        controller = AsyncioRunCancellationController()
        tool = PythonSubprocessTool(artifact_store=InMemoryArtifactStore())
        running = asyncio.create_task(
            tool.invoke(
                _invocation(PythonExecutionRequest(source="while True: pass")),
                controller.signal_for_attempt(),
            )
        )
        await started.wait()
        controller.request_cancel()
        return await running

    result = asyncio.run(scenario())
    assert result.status is ToolInvocationStatus.CANCELLED
    assert result.usage_certainty is UsageCertainty.UNKNOWN
    assert process.terminated


def test_python_subprocess_timeout_is_enforced_by_agent_runner(monkeypatch) -> None:
    class Process:
        returncode = None
        terminated = False

        class Stream:
            def __init__(self, process):
                self.process = process

            async def read(self, limit):
                del limit
                while self.process.returncode is None:
                    await asyncio.sleep(0)
                return b""

        def __init__(self):
            self.stdout = self.Stream(self)
            self.stderr = self.Stream(self)

        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -1

        def kill(self):
            self.returncode = -9

    process = Process()

    async def create(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    clock = FrozenClock()
    task = ResearchTask(
        task_id="task_python",
        perspective_id="perspective_one",
        objective="Run trusted calculation",
        required_capability_ids=("python",),
        expected_outputs=(
            ExpectedOutput(
                output_id="answer",
                description="answer",
                media_type="text/plain",
            ),
        ),
    )
    context = AgentContext(
        run_id="run_one",
        run_revision=4,
        dag_id="dag_one",
        task=task,
        task_id=task.task_id,
        attempt_id="attempt_one",
        attempt_number=1,
        task_operation_key=HASH_A,
        task_attempt_key=HASH_B,
        task_idempotency=IdempotencyMode.NON_IDEMPOTENT,
        deadline=clock.now() + timedelta(seconds=30),
        hard_limits=RuntimeResourceAmount(
            duration_milliseconds=30_000,
            tokens=100,
            cost_microunits=100,
            tool_calls=1,
        ),
        expected_outputs=task.expected_outputs,
        authorized_capability_ids=task.required_capability_ids,
    )
    agent = ScriptedAgent(
        _descriptor(),
        {
            AgentFixtureKey("task_python", 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_python",
                        capability_id="python",
                        input=PythonExecutionRequest(source="while True: pass"),
                    ),
                    usage=_usage(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    registry = CapabilityRegistry(
        allowed_modes=frozenset({AdapterMode.MOCK, AdapterMode.LOCAL})
    )
    registry.register(PythonSubprocessTool(artifact_store=InMemoryArtifactStore()))
    trace = InMemoryTraceSink()
    runner = AgentRunner(
        agent=agent,
        registry=registry,
        policy=AgentRunnerPolicy(max_agent_steps=2, max_tool_calls=1),
        clock=clock,
        sleeper=ImmediateSleeper(),
        trace_sink=trace,
        id_factory=SequentialIds(),
    )
    result = asyncio.run(
        runner.run(
            context, AsyncioRunCancellationController().signal_for_attempt()
        )
    )
    assert result.error is not None and result.error.code == "tool_timed_out"
    assert process.terminated
    persisted = "".join(
        event.model_dump_json() for event in trace.read(context.run_id)
    )
    assert "while True" not in persisted
    assert "PYTHONIOENCODING" not in persisted


def test_browser_search_contracts_and_exact_mock_fixture() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        BrowserRequest(url="file:///tmp/a")
    with pytest.raises(ValidationError):
        SearchHit(
            locator="x",
            url="not-a-url",
            title="title",
            snippet="",
            retrieved_at=now,
            adapter_id="mock_search",
        )
    browser = BrowserResult(
        adapter_id="mock_browser",
        final_url="https://example.com",
        title="Example",
        content="body",
        content_hash=hashlib.sha256(b"body").hexdigest(),
        retrieved_at=now,
    )
    assert browser.final_url == "https://example.com"

    descriptor = RecordingSearchTool().descriptor
    request = _invocation(SearchRequest(query="nothing"))
    empty = ToolInvocationResult(
        status=ToolInvocationStatus.SUCCEEDED,
        output=SearchResult(adapter_id="mock_search", hits=()),
        usage=ToolUsage(tool_calls=1),
        usage_certainty=UsageCertainty.EXACT,
    )
    mock = MockTool(
        descriptor,
        {ToolFixtureKey(HASH_A): ToolFixture(result=empty)},
    )
    result = asyncio.run(
        mock.invoke(
            request, AsyncioRunCancellationController().signal_for_attempt()
        )
    )
    assert result.output is not None and result.output.hits == ()
    missing = request.model_copy(update={"tool_operation_key": HASH_B})
    with pytest.raises(AgentContractError, match="no exact"):
        asyncio.run(
            mock.invoke(
                missing,
                AsyncioRunCancellationController().signal_for_attempt(),
            )
        )
