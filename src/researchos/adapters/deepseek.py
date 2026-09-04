"""DeepSeek implementations of existing provider-independent model ports."""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from researchos.adapters.openai_compatible import OpenAICompatibleChatTransport
from researchos.application.errors import (
    AgentContractError,
    PlanningModelFailure,
    RealProviderFailure,
    RunConfigurationError,
)
from researchos.application.real_composition import BoundRealModel
from researchos.configuration.real_settings import canonicalize_base_endpoint
from researchos.configuration.validation import (
    PHASE9B_AGENT_RESPONSE_CONTRACT,
    Phase9ARealAgentDecision,
    Phase9BRealAgentDecision,
    deepseek_prompt_content_hash,
    deepseek_response_contract,
    deepseek_system_prompt,
)
from researchos.domain.agent import (
    AgentDecision,
    AgentDescriptor,
    AgentError,
    AgentFailedDecision,
    AgentRequest,
)
from researchos.domain.planning import PlanningModelResponse, PlanningRequest
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.synthesis import (
    VerificationModelRequest,
    VerificationModelResponse,
)
from researchos.domain.tools import AdapterMode
from researchos.interfaces.providers import (
    ProviderAdmissionProfile,
    ProviderDispatchAuthorizer,
)
from researchos.interfaces.runtime import CancellationSignal


def _validate_binding(bound: BoundRealModel, role_id: str) -> None:
    settings = bound.settings
    if settings.provider_id != "deepseek" or settings.role_id != role_id:
        raise RunConfigurationError("DeepSeek adapter binding role/provider differs")
    if not canonicalize_base_endpoint(settings.base_endpoint).startswith("https://"):
        raise RunConfigurationError("DeepSeek base endpoint must use HTTPS")
    web_tools = (
        role_id == "agent"
        and settings.response_contract_version == PHASE9B_AGENT_RESPONSE_CONTRACT
    )
    if settings.prompt_content_hash != deepseek_prompt_content_hash(
        role_id, enable_web_tools=web_tools
    ):
        raise RunConfigurationError("DeepSeek prompt content hash differs")
    if settings.response_contract_version != deepseek_response_contract(
        role_id, enable_web_tools=web_tools
    ):
        raise RunConfigurationError("DeepSeek response contract differs")


def _messages(
    role_id: str, request: Any, *, enable_web_tools: bool = False
) -> tuple[dict[str, str], ...]:
    content = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        {
            "role": "system",
            "content": deepseek_system_prompt(
                role_id, enable_web_tools=enable_web_tools
            ),
        },
        {"role": "user", "content": content},
    )


class DeepSeekPlanningModel:
    def __init__(
        self,
        bound: BoundRealModel,
        transport: OpenAICompatibleChatTransport,
        authorizer: ProviderDispatchAuthorizer,
        *,
        trusted_runtime_replan: bool = False,
    ) -> None:
        _validate_binding(bound, "planning")
        self._bound = bound
        self._transport = transport
        self._authorizer = authorizer
        self._trusted_runtime_replan = trusted_runtime_replan

    def close(self) -> None:
        self._transport.close()

    def generate(self, request: PlanningRequest) -> PlanningModelResponse:
        if request.run_id != self._bound.run_id:
            raise PlanningModelFailure(
                "provider_run_mismatch", "planning request belongs to another Run"
            )
        self._authorizer.authorize(
            run_id=self._bound.run_id,
            role_id="planning",
            composition_hash=self._bound.composition_hash,
            trusted_runtime_replan=self._trusted_runtime_replan,
        )
        failure: PlanningModelFailure | None = None
        try:
            response = self._transport.complete(_messages("planning", request))
            payload = json.loads(response.content)
            if not isinstance(payload, dict):
                raise ValueError
            return PlanningModelResponse(
                planning_model_id=self._bound.settings.model_id,
                payload=payload,
            )
        except RealProviderFailure as exc:
            failure = PlanningModelFailure(
                exc.code,
                "planning provider failed",
                retryable=exc.retryable,
                usage=exc.usage,
                usage_certainty=exc.usage_certainty,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            failure = PlanningModelFailure(
                "provider_response_invalid",
                "planning provider response is invalid",
                retryable=False,
            )
        if failure is not None:
            raise failure
        raise AssertionError("planning provider exited without a result")


class DeepSeekAgent:
    def __init__(
        self,
        bound: BoundRealModel,
        transport: OpenAICompatibleChatTransport,
        authorizer: ProviderDispatchAuthorizer,
    ) -> None:
        _validate_binding(bound, "agent")
        self._bound = bound
        self._transport = transport
        self._authorizer = authorizer
        self._web_tools = (
            bound.settings.response_contract_version
            == PHASE9B_AGENT_RESPONSE_CONTRACT
        )
        self._descriptor = AgentDescriptor(
            agent_id="deepseek_agent",
            adapter_id=bound.settings.adapter_id,
            mode=AdapterMode.REAL,
            operation_version=bound.settings.adapter_version,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()

    @property
    def descriptor(self) -> AgentDescriptor:
        return self._descriptor

    @property
    def provider_call_reservation(self):
        return self._bound.settings.suboperation_reservation()

    @property
    def provider_admission_profile(self) -> ProviderAdmissionProfile:
        return (
            ProviderAdmissionProfile.ACCUMULATED_REMAINING_V1
            if self._web_tools
            else ProviderAdmissionProfile.LEGACY_TASK_LIMIT_V1
        )

    async def decide(
        self, request: AgentRequest, cancellation: CancellationSignal
    ) -> AgentDecision:
        if request.context.run_id != self._bound.run_id:
            raise AgentContractError("Agent request belongs to another Run")
        self._authorizer.authorize(
            run_id=self._bound.run_id,
            role_id="agent",
            composition_hash=self._bound.composition_hash,
        )
        if not self._web_tools:
            reservation = self._bound.settings.policy.provider_call_reservation
            if (
                reservation.total_tokens > request.context.hard_limits.tokens
                or reservation.cost_microunits
                > request.context.hard_limits.cost_microunits
            ):
                return AgentFailedDecision(
                    error=AgentError(
                        code="provider_call_reservation_exceeds_task_limit",
                        message="Provider-call reservation exceeds task hard limit",
                        retryable=False,
                    ),
                    usage=RuntimeResourceAmount(),
                    usage_certainty=UsageCertainty.EXACT,
                )
        try:
            response = await self._transport.complete_async(
                _messages("agent", request, enable_web_tools=self._web_tools),
                cancellation=cancellation,
                deadline=request.context.deadline,
            )
        except RealProviderFailure as exc:
            return AgentFailedDecision(
                error=AgentError(
                    code=exc.code,
                    message="DeepSeek Agent provider failed",
                    retryable=exc.retryable,
                ),
                usage=exc.usage,
                usage_certainty=exc.usage_certainty,
            )
        try:
            payload = json.loads(response.content)
            if not isinstance(payload, dict):
                raise ValueError
            payload["usage"] = response.usage.model_dump(mode="json")
            payload["usage_certainty"] = response.usage_certainty.value
            contract = (
                Phase9BRealAgentDecision
                if self._web_tools
                else Phase9ARealAgentDecision
            )
            return TypeAdapter(contract).validate_python(payload)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            ValidationError,
        ):
            return AgentFailedDecision(
                error=AgentError(
                    code="provider_response_invalid",
                    message="DeepSeek Agent response is invalid",
                    retryable=False,
                ),
                usage=response.usage,
                usage_certainty=response.usage_certainty,
            )


class DeepSeekVerificationModel:
    def __init__(
        self,
        bound: BoundRealModel,
        transport: OpenAICompatibleChatTransport,
        authorizer: ProviderDispatchAuthorizer,
    ) -> None:
        _validate_binding(bound, "verification")
        self._bound = bound
        self._transport = transport
        self._authorizer = authorizer

    async def aclose(self) -> None:
        await self._transport.aclose()

    @property
    def model_bundle_hash(self) -> str:
        return self._bound.bundle.model_bundle_hash

    async def invoke(
        self, request: VerificationModelRequest, cancellation: CancellationSignal
    ) -> VerificationModelResponse:
        self._authorizer.authorize(
            run_id=self._bound.run_id,
            role_id="verification",
            composition_hash=self._bound.composition_hash,
        )
        response = await self._transport.complete_async(
            _messages("verification", request), cancellation=cancellation
        )
        return VerificationModelResponse(
            raw_bytes=response.content,
            usage=response.usage,
            usage_certainty=response.usage_certainty,
            model_id=self._bound.settings.model_id,
            mode="real",
            role=request.role,
            round_number=request.round_number,
            draft_revision_id=request.draft_revision_id,
        )
