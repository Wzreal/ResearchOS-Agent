"""Construct REAL adapters only from a process-local composition binding."""

from __future__ import annotations

from collections.abc import Callable

from researchos.adapters.browser import (
    BoundedBrowserTool,
    PinnedHttpTransport,
    SystemHostResolver,
    TrafilaturaSubprocessExtractor,
)
from researchos.adapters.clock import SystemClock
from researchos.adapters.deepseek import (
    DeepSeekAgent,
    DeepSeekClaimExtractionModel,
    DeepSeekPlanningModel,
    DeepSeekVerificationModel,
)
from researchos.adapters.openai_compatible import OpenAICompatibleChatTransport
from researchos.adapters.tavily import HttpxTavilyTransport, TavilySearchTool
from researchos.adapters.zilliz_retrieval import (
    PymilvusZillizBm25Transport,
    ZillizBm25RetrievalTool,
)
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.errors import RunConfigurationError
from researchos.application.provider_dispatch import RunLifecycleDispatchAuthorizer
from researchos.application.real_composition import (
    BoundRealModel,
    RealCompositionManager,
)
from researchos.domain.contracts import TERMINAL_STATUSES
from researchos.domain.runtime import RuntimeResourceAmount
from researchos.domain.tools import AdapterMode
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
        self._run_store = run_store
        self._transport_factory = transport_factory
        self._authorizer = RunLifecycleDispatchAuthorizer(run_store, manager)
        self._registries: dict[str, CapabilityRegistry] = {}

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

    def trusted_runtime_replanning_model(self, run_id: str) -> DeepSeekPlanningModel:
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
        web_search_max_results = None
        if "web_search" in self._manager.configured_capability_ids():
            capability = self._manager.bound_capability(run_id, "web_search")
            policy = capability.settings.tavily_policy
            assert policy is not None
            web_search_max_results = policy.provider_request.max_results
        return DeepSeekAgent(
            bound,
            self._transport_factory(bound),
            self._authorizer,
            web_search_max_results=web_search_max_results,
        )

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

    def claim_extraction_model(
        self, run_id: str, *, workflow_budget_slice: RuntimeResourceAmount
    ) -> DeepSeekClaimExtractionModel:
        self._authorizer.authorize(run_id=run_id, role_id="claim_extraction")
        bound = self._manager.bound_model(run_id, "claim_extraction")
        self._authorizer.authorize(
            run_id=run_id,
            role_id="claim_extraction",
            composition_hash=bound.composition_hash,
        )
        return DeepSeekClaimExtractionModel(
            bound,
            self._transport_factory(bound),
            self._authorizer,
            workflow_budget_slice,
        )

    def capability_registry(self, run_id: str) -> CapabilityRegistry:
        state = self._run_store.load(run_id)
        if state.status in TERMINAL_STATUSES:
            self.evict_capability_registry(run_id)
            raise RunConfigurationError(
                "terminal Run cannot retain a REAL capability registry"
            )
        existing = self._registries.get(run_id)
        if existing is not None:
            return existing
        registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.REAL}))
        self._registries[run_id] = registry
        try:
            for capability_id in self._manager.configured_capability_ids():
                bound = self._manager.bound_capability(run_id, capability_id)
                from researchos.application.real_tool_dispatch import (
                    RealToolDispatchAuthorizer,
                )

                tool_authorizer = RealToolDispatchAuthorizer(
                    runs=self._run_store,
                    compositions=self._manager,
                    registry=registry,
                    bound=bound,
                )
                if capability_id == "web_search":
                    policy = bound.settings.tavily_policy
                    assert policy is not None
                    tool = TavilySearchTool(
                        bound=bound,
                        authorizer=tool_authorizer,
                        compositions=self._manager,
                        transport=HttpxTavilyTransport(policy.researchos),
                        clock=SystemClock(),
                    )
                elif capability_id == "web_browser":
                    policy = bound.settings.browser_policy
                    assert policy is not None
                    tool = BoundedBrowserTool(
                        bound=bound,
                        authorizer=tool_authorizer,
                        transport=PinnedHttpTransport(
                            SystemHostResolver(),
                            max_dns_answers=policy.max_dns_answers,
                            address_policy=policy.address_policy,
                        ),
                        extractor=TrafilaturaSubprocessExtractor(),
                        clock=SystemClock(),
                    )
                elif capability_id == "managed_retrieval":
                    tool = ZillizBm25RetrievalTool(
                        bound=bound,
                        authorizer=tool_authorizer,
                        compositions=self._manager,
                        transport=PymilvusZillizBm25Transport(),
                        clock=SystemClock(),
                    )
                else:  # pragma: no cover - settings validation is closed-world
                    raise ValueError("unsupported REAL capability")
                registry.register(tool)
            return registry
        except Exception:
            self._registries.pop(run_id, None)
            raise

    def evict_capability_registry(self, run_id: str) -> None:
        """Release the factory's process-local registry reference for a Run."""

        self._registries.pop(run_id, None)
