"""Bounded application-controlled Agent/Tool decision loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from math import ceil
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.errors import (
    AgentContractError,
    AgentTraceError,
    ToolPermissionDenied,
    UnknownCapabilityError,
)
from researchos.application.runtime_transitions import stable_key
from researchos.domain.agent import (
    AgentContext,
    AgentDecisionKind,
    AgentError,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentObservation,
    AgentRequest,
)
from researchos.domain.contracts import (
    TraceEvent,
    TraceEventType,
    canonical_json_bytes,
    model_sha256,
)
from researchos.domain.real_tools import (
    AuthorizedToolDispatchEnvelope,
    CapabilityDispatchContext,
)
from researchos.domain.runtime import (
    IdempotencyMode,
    RuntimeResourceAmount,
    UsageCertainty,
)
from researchos.domain.tools import (
    AdapterMode,
    ToolInvocationRequest,
    ToolInvocationResult,
    ToolInvocationStatus,
)
from researchos.interfaces.agent import Agent
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.interfaces.providers import (
    ProviderAdmissionProfile,
    ProviderAdmissionProfiledAgent,
    ProviderReservedAgent,
    ProviderReservedTool,
)
from researchos.interfaces.runtime import AsyncSleeper, CancellationSignal
from researchos.interfaces.tools import (
    AuthorizedRealTool,
    Phase3RetryDelegatingTool,
)
from researchos.security.redaction import PersistenceRedactor

IdFactory = Callable[[str], str]


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class AgentRunnerPolicy(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    max_agent_steps: int = Field(ge=1)
    max_tool_calls: int = Field(ge=0)
    max_observation_bytes: int = Field(default=1_000_000, ge=1)
    trace_failure_retryable: bool = True


class _UsageAccumulator:
    def __init__(self) -> None:
        self.amount = RuntimeResourceAmount()
        self.certainty = UsageCertainty.EXACT
        self.observations: list[AgentObservation] = []

    def add(
        self, amount: RuntimeResourceAmount | None, certainty: UsageCertainty
    ) -> None:
        if certainty is UsageCertainty.UNKNOWN:
            self.certainty = UsageCertainty.UNKNOWN
            return
        if self.certainty is UsageCertainty.UNKNOWN:
            return
        assert amount is not None
        self.amount = self.amount.plus(amount)
        if certainty is UsageCertainty.UPPER_BOUND:
            self.certainty = UsageCertainty.UPPER_BOUND

    def values(self) -> tuple[RuntimeResourceAmount | None, UsageCertainty]:
        if self.certainty is UsageCertainty.UNKNOWN:
            return None, self.certainty
        return self.amount, self.certainty

    def mark_unknown(self) -> None:
        self.certainty = UsageCertainty.UNKNOWN


class AgentRunner:
    def __init__(
        self,
        *,
        agent: Agent,
        registry: CapabilityRegistry,
        policy: AgentRunnerPolicy,
        clock: Clock,
        sleeper: AsyncSleeper,
        trace_sink: TraceSink,
        id_factory: IdFactory | None = None,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._agent = agent
        self._registry = registry
        self._registry.require_mode(agent.descriptor.mode)
        self._policy = policy
        self._clock = clock
        self._sleeper = sleeper
        self._trace = trace_sink
        self._id_factory = id_factory or _default_id_factory
        self._redactor = redactor or PersistenceRedactor()

    async def run(
        self, context: AgentContext, cancellation: CancellationSignal
    ) -> AgentExecutionResult:
        result = await self._run(context, cancellation)
        if result.status is AgentExecutionStatus.FAILED:
            assert result.error is not None
            with suppress(AgentTraceError):
                self._emit(
                    context,
                    TraceEventType.AGENT_FAILED,
                    correlation_id=context.attempt_id,
                    attributes={
                        "failure_code": result.error.code,
                        "retryable": result.error.retryable,
                    },
                )
        return result

    async def _run(
        self, context: AgentContext, cancellation: CancellationSignal
    ) -> AgentExecutionResult:
        usage = _UsageAccumulator()
        observations = usage.observations
        effective_tool_limit = min(
            self._policy.max_tool_calls, context.hard_limits.tool_calls
        )
        try:
            previous_event = self._emit(
                context,
                TraceEventType.AGENT_STARTED,
                correlation_id=context.attempt_id,
                attributes={
                    "agent_id": self._agent.descriptor.agent_id,
                    "adapter_id": self._agent.descriptor.adapter_id,
                    "attempt_id": context.attempt_id,
                },
            )
        except AgentTraceError:
            return self._failure(
                "agent_trace_start_failed",
                "agent start trace could not be persisted",
                usage,
                retryable=(
                    self._policy.trace_failure_retryable
                    and context.task_idempotency is IdempotencyMode.IDEMPOTENT
                ),
            )

        for step in range(1, self._policy.max_agent_steps + 1):
            precondition = self._precondition_failure(context, cancellation, usage)
            if precondition is not None:
                return precondition
            if isinstance(self._agent, ProviderReservedAgent):
                profile = self._agent_admission_profile()
                if profile is ProviderAdmissionProfile.ACCUMULATED_REMAINING_V1:
                    admission = self._reservation_failure(
                        "agent",
                        self._agent.provider_call_reservation,
                        usage,
                        context.hard_limits,
                        effective_tool_limit,
                    )
                    if admission is not None:
                        return admission
            elif self._agent.descriptor.mode is AdapterMode.REAL:
                return self._failure(
                    "provider_reservation_contract_invalid",
                    "REAL Agent does not expose provider reservation authority",
                    usage,
                )
            request = AgentRequest(
                request_id=self._id_factory("areq"),
                context=context,
                agent_step=step,
                observations=tuple(observations),
            )
            outcome, value = await self._await_bounded(
                self._agent.decide(request, cancellation),
                deadline=context.deadline,
                cancellation=cancellation,
            )
            if outcome == "cancelled":
                usage.mark_unknown()
                return self._failure(
                    "agent_cancelled", "agent decision was cancelled", usage
                )
            if outcome == "timed_out":
                usage.mark_unknown()
                return self._failure(
                    "agent_decision_timed_out",
                    "agent decision exceeded the task deadline",
                    usage,
                )
            if outcome == "error":
                usage.mark_unknown()
                code = (
                    "agent_malformed_decision"
                    if isinstance(value, AgentContractError)
                    else "agent_decision_failed"
                )
                return self._failure(
                    code,
                    "agent decision adapter failed",
                    usage,
                )
            decision = value
            if not hasattr(decision, "kind"):
                return self._failure(
                    "agent_malformed_decision",
                    "agent returned a malformed decision",
                    usage,
                )
            usage.add(decision.usage, decision.usage_certainty)
            if usage.certainty is UsageCertainty.UNKNOWN:
                if decision.kind is AgentDecisionKind.FAILED:
                    return self._failure(
                        decision.error.code,
                        decision.error.message,
                        usage,
                        retryable=decision.error.retryable,
                    )
                return self._failure(
                    "agent_usage_unknown",
                    "agent usage could not be determined",
                    usage,
                )
            if self._over_limit(usage.amount, context.hard_limits):
                return self._failure(
                    "agent_hard_limit_exceeded",
                    "agent usage exceeded task hard limits",
                    usage,
                )
            try:
                previous_event = self._emit(
                    context,
                    TraceEventType.AGENT_DECISION,
                    correlation_id=context.attempt_id,
                    causation_id=previous_event.event_id,
                    attributes={"agent_step": step, "kind": decision.kind.value},
                )
            except AgentTraceError:
                return self._failure(
                    "agent_trace_terminal_failed",
                    "agent decision trace could not be persisted",
                    usage,
                    retryable=(
                        self._policy.trace_failure_retryable
                        and context.task_idempotency is IdempotencyMode.IDEMPOTENT
                    ),
                )

            if decision.kind is AgentDecisionKind.FINAL:
                try:
                    self._emit(
                        context,
                        TraceEventType.AGENT_COMPLETED,
                        correlation_id=context.attempt_id,
                        causation_id=previous_event.event_id,
                        attributes={
                            "output_ids": [
                                item.output_id for item in decision.final.outputs
                            ]
                        },
                    )
                except AgentTraceError:
                    return self._failure(
                        "agent_trace_terminal_failed",
                        "agent completion trace could not be persisted",
                        usage,
                        retryable=(
                            self._policy.trace_failure_retryable
                            and context.task_idempotency is IdempotencyMode.IDEMPOTENT
                        ),
                    )
                amount, certainty = usage.values()
                return AgentExecutionResult(
                    status=AgentExecutionStatus.SUCCEEDED,
                    final=decision.final,
                    usage=amount,
                    usage_certainty=certainty,
                    backend_receipt=f"agent_{context.task_attempt_key[:32]}",
                    observations=tuple(observations),
                )
            if decision.kind is AgentDecisionKind.FAILED:
                return self._failure(
                    decision.error.code,
                    decision.error.message,
                    usage,
                    retryable=decision.error.retryable,
                )

            tool_call = decision.tool_call
            if usage.amount.tool_calls >= effective_tool_limit:
                return self._failure(
                    "agent_tool_call_limit_exceeded",
                    "agent tool-call limit was exhausted",
                    usage,
                )
            try:
                tool = self._registry.resolve_authorized(
                    tool_call.capability_id, context.authorized_capability_ids
                )
            except ToolPermissionDenied:
                self._permission_trace(context, step, tool_call.tool_call_id)
                return self._failure(
                    "tool_permission_denied",
                    "task did not authorize the requested capability",
                    usage,
                )
            except UnknownCapabilityError:
                return self._failure(
                    "unknown_capability",
                    "requested capability is not registered",
                    usage,
                )
            descriptor = tool.descriptor
            if descriptor.input_type != tool_call.input.input_type:
                return self._failure(
                    "tool_input_type_mismatch",
                    "tool input does not match the resolved descriptor",
                    usage,
                )
            if (
                descriptor.idempotency is IdempotencyMode.NON_IDEMPOTENT
                and context.task_idempotency is IdempotencyMode.IDEMPOTENT
            ):
                return self._failure(
                    "tool_idempotency_mismatch",
                    "non-idempotent tool requires a non-idempotent task",
                    usage,
                )
            if isinstance(tool, ProviderReservedTool):
                admission = self._reservation_failure(
                    "tool",
                    tool.provider_call_reservation,
                    usage,
                    context.hard_limits,
                    effective_tool_limit,
                )
                if admission is not None:
                    return admission
            elif descriptor.mode is AdapterMode.REAL:
                return self._failure(
                    "provider_reservation_contract_invalid",
                    "REAL Tool does not expose provider reservation authority",
                    usage,
                )
            try:
                self._redactor.assert_safe_model(tool_call.input)
            except Exception:
                return self._failure(
                    "unsafe_tool_input", "tool input is unsafe to trace", usage
                )
            input_hash = model_sha256(tool_call.input)
            operation_key = stable_key(
                {
                    "task_operation_key": context.task_operation_key,
                    "agent_step": step,
                    "capability_id": descriptor.capability_id,
                    "tool_id": descriptor.tool_id,
                    "adapter_id": descriptor.adapter_id,
                    "tool_operation_version": descriptor.operation_version,
                    "safe_input_hash": input_hash,
                }
            )
            invocation_id = f"toolop_{operation_key[:32]}"
            invocation = ToolInvocationRequest(
                invocation_id=invocation_id,
                run_id=context.run_id,
                task_id=context.task_id,
                attempt_id=context.attempt_id,
                agent_step=step,
                tool_call_id=tool_call.tool_call_id,
                tool_operation_key=operation_key,
                deadline=context.deadline,
                input=tool_call.input,
            )
            if descriptor.mode is AdapterMode.REAL:
                if not isinstance(tool, AuthorizedRealTool):
                    return self._failure(
                        "real_tool_authorization_contract_invalid",
                        "REAL Tool does not expose authorized dispatch",
                        usage,
                    )
                dispatch_context = CapabilityDispatchContext.build(
                    run_id=context.run_id,
                    task=context.task,
                    task_id=context.task_id,
                    task_contract_hash=model_sha256(context.task),
                    task_operation_key=context.task_operation_key,
                    requested_capability_id=descriptor.capability_id,
                    authorized_capability_ids=context.authorized_capability_ids,
                    descriptor_hash=model_sha256(descriptor),
                    tool_operation_key=operation_key,
                )
                authorized = AuthorizedToolDispatchEnvelope(
                    invocation=invocation,
                    dispatch_context=dispatch_context,
                )
                def tool_invoke(
                    resolved_tool=tool,
                    authorized_envelope=authorized,
                    signal=cancellation,
                ):
                    return resolved_tool.invoke_authorized(authorized_envelope, signal)
            else:
                def tool_invoke(
                    resolved_tool=tool,
                    tool_invocation=invocation,
                    signal=cancellation,
                ):
                    return resolved_tool.invoke(tool_invocation, signal)
            try:
                requested = self._emit(
                    context,
                    TraceEventType.AGENT_TOOL_REQUESTED,
                    correlation_id=invocation_id,
                    causation_id=previous_event.event_id,
                    attributes=self._tool_attributes(invocation, descriptor),
                )
                started = self._emit(
                    context,
                    TraceEventType.TOOL_INVOCATION_STARTED,
                    correlation_id=invocation_id,
                    causation_id=requested.event_id,
                    attributes=self._tool_attributes(invocation, descriptor),
                )
            except AgentTraceError:
                return self._failure(
                    "tool_trace_start_failed",
                    "tool start trace could not be persisted",
                    usage,
                    retryable=(
                        self._policy.trace_failure_retryable
                        and context.task_idempotency is IdempotencyMode.IDEMPOTENT
                        and descriptor.idempotency is IdempotencyMode.IDEMPOTENT
                    ),
                )
            tool_outcome, tool_value = await self._await_bounded(
                tool_invoke(),
                deadline=context.deadline,
                cancellation=cancellation,
            )
            if tool_outcome in {"cancelled", "timed_out"}:
                event_type = (
                    TraceEventType.TOOL_INVOCATION_CANCELLED
                    if tool_outcome == "cancelled"
                    else TraceEventType.TOOL_INVOCATION_TIMED_OUT
                )
                try:
                    self._emit(
                        context,
                        event_type,
                        correlation_id=invocation_id,
                        causation_id=started.event_id,
                        attributes=self._tool_attributes(invocation, descriptor),
                    )
                except AgentTraceError:
                    return self._failure(
                        "tool_trace_terminal_failed",
                        "tool terminal trace could not be persisted after dispatch",
                        _unknown_usage(),
                        retryable=(
                            self._policy.trace_failure_retryable
                            and context.task_idempotency is IdempotencyMode.IDEMPOTENT
                            and descriptor.idempotency is IdempotencyMode.IDEMPOTENT
                        ),
                    )
                return self._failure(
                    f"tool_{tool_outcome}",
                    f"tool invocation {tool_outcome}",
                    _unknown_usage(),
                )
            if tool_outcome == "error":
                result = ToolInvocationResult(
                    status=ToolInvocationStatus.FAILED,
                    error={
                        "code": "tool_adapter_exception",
                        "message": "tool adapter raised an exception",
                    },
                    usage=None,
                    usage_certainty=UsageCertainty.UNKNOWN,
                )
            else:
                result = tool_value
            if not isinstance(result, ToolInvocationResult):
                usage.mark_unknown()
                try:
                    self._emit(
                        context,
                        TraceEventType.TOOL_INVOCATION_FAILED,
                        correlation_id=invocation_id,
                        causation_id=started.event_id,
                        attributes={
                            **self._tool_attributes(invocation, descriptor),
                            "status": "contract_violation",
                            "failure_code": "tool_output_contract_violation",
                            "usage_certainty": UsageCertainty.UNKNOWN.value,
                        },
                    )
                except AgentTraceError:
                    return self._failure(
                        "tool_trace_terminal_failed",
                        "tool terminal trace could not be persisted after execution",
                        usage,
                        retryable=(
                            self._policy.trace_failure_retryable
                            and self._tool_retryable(context, descriptor)
                        ),
                    )
                return self._failure(
                    "tool_output_contract_violation",
                    "tool returned an invalid result contract",
                    usage,
                    retryable=self._tool_retryable(context, descriptor),
                )
            usage.add(_runtime_usage(result.usage), result.usage_certainty)
            integrity_failure = self._tool_result_integrity_failure(
                result, descriptor, operation_key
            )
            if integrity_failure is not None:
                code, message = integrity_failure
                try:
                    self._emit(
                        context,
                        TraceEventType.TOOL_INVOCATION_FAILED,
                        correlation_id=invocation_id,
                        causation_id=started.event_id,
                        attributes={
                            **self._tool_attributes(invocation, descriptor),
                            "status": "contract_violation",
                            "failure_code": code,
                            "usage_certainty": result.usage_certainty.value,
                        },
                    )
                except AgentTraceError:
                    return self._failure(
                        "tool_trace_terminal_failed",
                        "tool terminal trace could not be persisted after execution",
                        usage,
                        retryable=(
                            self._policy.trace_failure_retryable
                            and self._tool_retryable(context, descriptor)
                        ),
                    )
                return self._failure(
                    code,
                    message,
                    usage,
                    retryable=self._tool_retryable(context, descriptor),
                )
            if len(canonical_json_bytes(result)) > self._policy.max_observation_bytes:
                try:
                    self._emit(
                        context,
                        TraceEventType.TOOL_INVOCATION_FAILED,
                        correlation_id=invocation_id,
                        causation_id=started.event_id,
                        attributes={
                            **self._tool_attributes(invocation, descriptor),
                            "status": "result_too_large",
                            "failure_code": "tool_result_too_large",
                            "usage_certainty": result.usage_certainty.value,
                        },
                    )
                except AgentTraceError:
                    return self._failure(
                        "tool_trace_terminal_failed",
                        "tool terminal trace could not be persisted after execution",
                        usage,
                        retryable=(
                            self._policy.trace_failure_retryable
                            and self._tool_retryable(context, descriptor)
                        ),
                    )
                return self._failure(
                    "tool_result_too_large",
                    "tool result exceeds the observation byte limit",
                    usage,
                    retryable=self._tool_retryable(context, descriptor),
                )
            terminal_type = {
                ToolInvocationStatus.SUCCEEDED: (
                    TraceEventType.TOOL_INVOCATION_SUCCEEDED
                ),
                ToolInvocationStatus.FAILED: TraceEventType.TOOL_INVOCATION_FAILED,
                ToolInvocationStatus.CANCELLED: (
                    TraceEventType.TOOL_INVOCATION_CANCELLED
                ),
                ToolInvocationStatus.TIMED_OUT: (
                    TraceEventType.TOOL_INVOCATION_TIMED_OUT
                ),
            }[result.status]
            try:
                terminal = self._emit(
                    context,
                    terminal_type,
                    correlation_id=invocation_id,
                    causation_id=started.event_id,
                    attributes={
                        **self._tool_attributes(invocation, descriptor),
                        "status": result.status.value,
                        "usage_certainty": result.usage_certainty.value,
                        "artifact_ids": [item.artifact_id for item in result.artifacts],
                    },
                )
            except AgentTraceError:
                return self._failure(
                    "tool_trace_terminal_failed",
                    "tool terminal trace could not be persisted after execution",
                    usage,
                    retryable=(
                        self._policy.trace_failure_retryable
                        and context.task_idempotency is IdempotencyMode.IDEMPOTENT
                        and descriptor.idempotency is IdempotencyMode.IDEMPOTENT
                    ),
                )
            observation = AgentObservation(
                agent_step=step,
                tool_call_id=tool_call.tool_call_id,
                capability_id=descriptor.capability_id,
                tool_id=descriptor.tool_id,
                adapter_id=descriptor.adapter_id,
                tool_input_hash=input_hash,
                tool_operation_key=operation_key,
                result=result,
            )
            if result.status is ToolInvocationStatus.SUCCEEDED:
                observations.append(observation)
            if result.status in {
                ToolInvocationStatus.CANCELLED,
                ToolInvocationStatus.TIMED_OUT,
            }:
                return self._failure(
                    f"tool_{result.status.value}",
                    result.error.message,
                    usage,
                )
            if usage.certainty is UsageCertainty.UNKNOWN:
                return self._failure(
                    "tool_usage_unknown",
                    "tool usage could not be determined",
                    usage,
                )
            if self._over_limit(usage.amount, context.hard_limits):
                return self._failure(
                    "agent_hard_limit_exceeded",
                    "tool usage exceeded task hard limits",
                    usage,
                )
            if result.status is not ToolInvocationStatus.SUCCEEDED:
                observations.append(observation)
                assert result.error is not None
                if (
                    result.error.retryable
                    and isinstance(tool, Phase3RetryDelegatingTool)
                    and tool.delegates_retry_to_phase3
                ):
                    return self._failure(
                        result.error.code,
                        result.error.message,
                        usage,
                        retryable=self._tool_retryable(context, descriptor),
                    )
            previous_event = terminal

        return self._failure(
            "agent_max_steps_exceeded",
            "agent did not finish within max_agent_steps",
            usage,
        )

    async def _await_bounded(
        self,
        awaitable,
        *,
        deadline: datetime,
        cancellation: CancellationSignal,
    ) -> tuple[str, Any]:
        if cancellation.cancelled:
            if hasattr(awaitable, "close"):
                awaitable.close()
            return "cancelled", None
        remaining = ceil((deadline - self._clock.now()).total_seconds() * 1_000)
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            return "timed_out", None
        operation = asyncio.create_task(awaitable)
        cancelled = asyncio.create_task(cancellation.wait())
        timed_out = asyncio.create_task(self._sleeper.sleep(remaining))
        try:
            done, _ = await asyncio.wait(
                {operation, cancelled, timed_out},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                operation.cancel()
                timed_out.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await operation
                with suppress(asyncio.CancelledError):
                    await timed_out
                return "cancelled", None
            if operation in done:
                cancelled.cancel()
                timed_out.cancel()
                with suppress(asyncio.CancelledError):
                    await cancelled
                with suppress(asyncio.CancelledError):
                    await timed_out
                try:
                    return "completed", operation.result()
                except asyncio.CancelledError:
                    return "cancelled", None
                except Exception as exc:
                    return "error", exc
            operation.cancel()
            with suppress(asyncio.CancelledError):
                await operation
            cancelled.cancel()
            with suppress(asyncio.CancelledError):
                await cancelled
            return "timed_out", None
        except asyncio.CancelledError:
            for task in (operation, cancelled, timed_out):
                task.cancel()
            for task in (operation, cancelled, timed_out):
                with suppress(asyncio.CancelledError, Exception):
                    await task
            raise

    def _precondition_failure(self, context, cancellation, usage):
        if cancellation.cancelled:
            return self._failure(
                "agent_cancelled", "agent execution was cancelled", usage
            )
        if self._clock.now() >= context.deadline:
            return self._failure(
                "agent_deadline_exhausted", "task deadline was exhausted", usage
            )
        return None

    def _reservation_failure(
        self,
        boundary,
        reservation,
        usage,
        hard_limits,
        effective_tool_limit,
    ):
        amount = usage.amount
        checks = (
            (
                "duration",
                amount.duration_milliseconds + reservation.duration_milliseconds,
                hard_limits.duration_milliseconds,
            ),
            ("tokens", amount.tokens + reservation.tokens, hard_limits.tokens),
            (
                "cost",
                amount.cost_microunits + reservation.cost_microunits,
                hard_limits.cost_microunits,
            ),
        )
        for resource, candidate, limit in checks:
            if candidate > limit:
                return self._failure(
                    f"{boundary}_provider_reservation_exceeds_remaining_{resource}",
                    f"{boundary} provider reservation exceeds remaining hard limit",
                    usage,
                )
        tool_limit = min(hard_limits.tool_calls, effective_tool_limit)
        if amount.tool_calls + reservation.tool_calls > tool_limit:
            return self._failure(
                "tool_call_reservation_unavailable",
                "provider reservation exceeds remaining Tool-call allowance",
                usage,
            )
        return None

    def _agent_admission_profile(self) -> ProviderAdmissionProfile:
        if isinstance(self._agent, ProviderAdmissionProfiledAgent):
            return self._agent.provider_admission_profile
        return ProviderAdmissionProfile.ACCUMULATED_REMAINING_V1

    @staticmethod
    def _over_limit(
        amount: RuntimeResourceAmount, limits: RuntimeResourceAmount
    ) -> bool:
        return not amount.fits_within(limits)

    def _failure(self, code, message, usage, *, retryable=False):
        amount, certainty = usage.values()
        return AgentExecutionResult(
            status=AgentExecutionStatus.FAILED,
            error=AgentError(code=code, message=message, retryable=retryable),
            usage=amount,
            usage_certainty=certainty,
            observations=tuple(usage.observations),
        )

    def _emit(
        self,
        context: AgentContext,
        event_type: TraceEventType,
        *,
        correlation_id: str,
        attributes: dict[str, Any],
        causation_id: str | None = None,
    ) -> TraceEvent:
        safe = self._redactor.redact_value(attributes)
        assert isinstance(safe, dict)
        event = TraceEvent(
            event_id=self._id_factory("evt"),
            event_type=event_type,
            timestamp=self._clock.now(),
            run_id=context.run_id,
            revision=context.run_revision,
            correlation_id=correlation_id,
            causation_id=causation_id,
            attributes=safe,
        )
        try:
            self._trace.append(event)
        except Exception as exc:
            raise AgentTraceError("agent/tool trace append failed") from exc
        return event

    def _permission_trace(self, context, step, tool_call_id):
        with suppress(AgentTraceError):
            self._emit(
                context,
                TraceEventType.TOOL_PERMISSION_DENIED,
                correlation_id=context.attempt_id,
                attributes={"agent_step": step, "tool_call_id": tool_call_id},
            )

    @staticmethod
    def _tool_attributes(invocation, descriptor):
        return {
            "invocation_id": invocation.invocation_id,
            "tool_call_id": invocation.tool_call_id,
            "tool_operation_key": invocation.tool_operation_key,
            "agent_step": invocation.agent_step,
            "capability_id": descriptor.capability_id,
            "tool_id": descriptor.tool_id,
            "adapter_id": descriptor.adapter_id,
            "schema_version": descriptor.schema_version,
        }

    @staticmethod
    def _tool_retryable(context, descriptor) -> bool:
        return (
            context.task_idempotency is IdempotencyMode.IDEMPOTENT
            and descriptor.idempotency is IdempotencyMode.IDEMPOTENT
        )

    @staticmethod
    def _tool_result_integrity_failure(result, descriptor, operation_key):
        if result.status is ToolInvocationStatus.SUCCEEDED:
            if result.output.output_type != descriptor.output_type:
                return (
                    "tool_output_contract_violation",
                    "tool output does not match the resolved descriptor",
                )
            adapter_id = getattr(result.output, "adapter_id", None)
            if adapter_id is not None and adapter_id != descriptor.adapter_id:
                return (
                    "tool_provenance_mismatch",
                    "tool output adapter identity does not match the descriptor",
                )
        if any(
            artifact.producer_tool_id != descriptor.tool_id
            or artifact.tool_operation_key != operation_key
            for artifact in result.artifacts
        ):
            return (
                "tool_provenance_mismatch",
                "tool artifact provenance does not match the invocation",
            )
        return None


def _runtime_usage(usage):
    if usage is None:
        return None
    return RuntimeResourceAmount(
        duration_milliseconds=usage.duration_milliseconds,
        tokens=usage.tokens,
        cost_microunits=usage.cost_microunits,
        tool_calls=usage.tool_calls,
    )


def _unknown_usage():
    value = _UsageAccumulator()
    value.add(None, UsageCertainty.UNKNOWN)
    return value
