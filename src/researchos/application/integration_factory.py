"""Construct REAL adapters only from a process-local composition binding."""

from __future__ import annotations

from collections.abc import Callable

from researchos.adapters.deepseek import (
    DeepSeekAgent,
    DeepSeekPlanningModel,
    DeepSeekVerificationModel,
)
from researchos.adapters.openai_compatible import OpenAICompatibleChatTransport
from researchos.application.provider_dispatch import RunLifecycleDispatchAuthorizer
from researchos.application.real_composition import (
    BoundRealModel,
    RealCompositionManager,
)
from researchos.interfaces.lifecycle import RunStore

TransportFactory = Callable[[BoundRealModel], OpenAICompatibleChatTransport]


class RealIntegrationFactory:
    def __init__(
        self,
        manager: RealCompositionManager,
        *,
        run_store: RunStore,
        transport_factory: TransportFactory = OpenAICompatibleChatTransport,
    ) -> None:
        self._manager = manager
        self._transport_factory = transport_factory
        self._authorizer = RunLifecycleDispatchAuthorizer(run_store, manager)

    def planning_model(self, run_id: str) -> DeepSeekPlanningModel:
        self._authorizer.authorize(run_id=run_id, role_id="planning")
        bound = self._manager.bound_model(run_id, "planning")
        self._authorizer.authorize(
            run_id=run_id,
            role_id="planning",
            composition_hash=bound.composition_hash,
        )
        return DeepSeekPlanningModel(
            bound, self._transport_factory(bound), self._authorizer
        )

    def trusted_runtime_replanning_model(
        self, run_id: str
    ) -> DeepSeekPlanningModel:
        self._authorizer.authorize(
            run_id=run_id, role_id="planning", trusted_runtime_replan=True
        )
        bound = self._manager.bound_model(run_id, "planning")
        self._authorizer.authorize(
            run_id=run_id,
            role_id="planning",
            composition_hash=bound.composition_hash,
            trusted_runtime_replan=True,
        )
        return DeepSeekPlanningModel(
            bound,
            self._transport_factory(bound),
            self._authorizer,
            trusted_runtime_replan=True,
        )

    def agent(self, run_id: str) -> DeepSeekAgent:
        self._authorizer.authorize(run_id=run_id, role_id="agent")
        bound = self._manager.bound_model(run_id, "agent")
        self._authorizer.authorize(
            run_id=run_id,
            role_id="agent",
            composition_hash=bound.composition_hash,
        )
        return DeepSeekAgent(bound, self._transport_factory(bound), self._authorizer)

    def verification_model(self, run_id: str) -> DeepSeekVerificationModel:
        self._authorizer.authorize(run_id=run_id, role_id="verification")
        bound = self._manager.bound_model(run_id, "verification")
        self._authorizer.authorize(
            run_id=run_id,
            role_id="verification",
            composition_hash=bound.composition_hash,
        )
        return DeepSeekVerificationModel(
            bound, self._transport_factory(bound), self._authorizer
        )
