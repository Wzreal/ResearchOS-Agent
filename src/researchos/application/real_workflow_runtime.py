"""Lazy REAL adapter binding for the existing Phase 10 workflow path.

The durable REAL composition is keyed by a Run id, which does not exist until
``RunManager.create`` completes.  These small delegates keep that unavoidable
late binding out of the workflow coordinator and never persist process-local
adapter objects.
"""

from __future__ import annotations

from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.errors import RunConfigurationError
from researchos.application.integration_factory import RealIntegrationFactory
from researchos.domain.agent import AgentDescriptor, AgentRequest
from researchos.domain.claim_extraction import ClaimExtractionRequest
from researchos.domain.contracts import RunState
from researchos.domain.planning import PlanningModelResponse, PlanningRequest
from researchos.domain.real_composition import ProviderSuboperationReservation
from researchos.domain.real_tools import AuthorizedToolDispatchEnvelope
from researchos.domain.synthesis import (
    VerificationModelRequest,
    VerificationModelResponse,
)
from researchos.domain.tools import (
    ToolDescriptor,
    ToolInvocationRequest,
    ToolInvocationResult,
)
from researchos.interfaces.runtime import CancellationSignal


class RealWorkflowRuntime:
    """Bind existing REAL adapters to one currently executing durable Run."""

    def __init__(
        self,
        *,
        integrations: RealIntegrationFactory,
        agent_descriptor: AgentDescriptor,
        tool_descriptors: tuple[ToolDescriptor, ...],
        claim_budget,
    ) -> None:
        self._integrations = integrations
        self._agent_descriptor = agent_descriptor
        self._tool_descriptors = {item.capability_id: item for item in tool_descriptors}
        self._claim_budget = claim_budget
        self._run_id: str | None = None
        self._registry: CapabilityRegistry | None = None

    def bind(self, state: RunState) -> None:
        if state.config.mode.value != "real":
            raise RunConfigurationError("REAL runtime requires a REAL Run")
        if self._run_id not in {None, state.run_id}:
            raise RunConfigurationError("REAL runtime cannot bind multiple Runs")
        self._run_id = state.run_id
        self._registry = self._integrations.capability_registry(state.run_id)

    def _require_run_id(self) -> str:
        if self._run_id is None:
            raise RunConfigurationError("REAL runtime has not been bound to a Run")
        return self._run_id

    @property
    def agent_descriptor(self) -> AgentDescriptor:
        return self._agent_descriptor

    @property
    def tool_descriptors(self) -> tuple[ToolDescriptor, ...]:
        return tuple(
            self._tool_descriptors[key] for key in sorted(self._tool_descriptors)
        )

    def planning(self, request: PlanningRequest) -> PlanningModelResponse:
        return self._integrations.planning_model(self._require_run_id()).generate(
            request
        )

    async def agent(self, request: AgentRequest, cancellation: CancellationSignal):
        return await self._integrations.agent(self._require_run_id()).decide(
            request, cancellation
        )

    def agent_provider_call_reservation(self) -> ProviderSuboperationReservation:
        return self._integrations.agent(
            self._require_run_id()
        ).provider_call_reservation

    def agent_provider_admission_profile(self):
        return self._integrations.agent(
            self._require_run_id()
        ).provider_admission_profile

    def claim_extraction(self, request: ClaimExtractionRequest):
        return self._integrations.claim_extraction_model(
            self._require_run_id(), workflow_budget_slice=self._claim_budget
        ).generate(request)

    async def verification(
        self, request: VerificationModelRequest, cancellation: CancellationSignal
    ) -> VerificationModelResponse:
        return await self._integrations.verification_model(
            self._require_run_id()
        ).invoke(request, cancellation)

    async def tool(
        self,
        capability_id: str,
        request: ToolInvocationRequest,
        cancellation: CancellationSignal,
    ) -> ToolInvocationResult:
        if request.run_id != self._require_run_id():
            raise RunConfigurationError("REAL tool request belongs to another Run")
        if self._registry is None:
            raise RunConfigurationError("REAL capability registry is unavailable")
        return await self._registry.resolve(capability_id).invoke(request, cancellation)


class RealPlanningModel:
    def __init__(self, runtime: RealWorkflowRuntime) -> None:
        self._runtime = runtime

    def generate(self, request: PlanningRequest) -> PlanningModelResponse:
        return self._runtime.planning(request)


class RealAgent:
    def __init__(self, runtime: RealWorkflowRuntime) -> None:
        self._runtime = runtime

    @property
    def descriptor(self) -> AgentDescriptor:
        return self._runtime.agent_descriptor

    @property
    def provider_call_reservation(self) -> ProviderSuboperationReservation:
        return self._runtime.agent_provider_call_reservation()

    @property
    def provider_admission_profile(self):
        return self._runtime.agent_provider_admission_profile()

    async def decide(self, request: AgentRequest, cancellation: CancellationSignal):
        return await self._runtime.agent(request, cancellation)


class RealClaimExtractionModel:
    def __init__(self, runtime: RealWorkflowRuntime, *, model_bundle_hash: str) -> None:
        self._runtime = runtime
        self._model_bundle_hash = model_bundle_hash

    @property
    def model_bundle_hash(self) -> str:
        return self._model_bundle_hash

    def generate(self, request: ClaimExtractionRequest):
        return self._runtime.claim_extraction(request)


class RealVerificationModel:
    def __init__(self, runtime: RealWorkflowRuntime, *, model_bundle_hash: str) -> None:
        self._runtime = runtime
        self._model_bundle_hash = model_bundle_hash

    @property
    def model_bundle_hash(self) -> str:
        return self._model_bundle_hash

    async def invoke(
        self, request: VerificationModelRequest, cancellation: CancellationSignal
    ) -> VerificationModelResponse:
        return await self._runtime.verification(request, cancellation)


class RealTool:
    def __init__(
        self, runtime: RealWorkflowRuntime, descriptor: ToolDescriptor
    ) -> None:
        self._runtime = runtime
        self._descriptor = descriptor

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    @property
    def provider_call_reservation(self) -> ProviderSuboperationReservation:
        registry = self._runtime._registry
        if registry is None:
            raise RunConfigurationError("REAL capability registry is unavailable")
        reservation = registry.resolve(
            self._descriptor.capability_id
        ).provider_call_reservation
        if not isinstance(reservation, ProviderSuboperationReservation):
            raise RunConfigurationError("REAL Tool reservation authority is invalid")
        return reservation

    async def invoke(
        self, request: ToolInvocationRequest, cancellation: CancellationSignal
    ) -> ToolInvocationResult:
        return await self._runtime.tool(
            self._descriptor.capability_id, request, cancellation
        )

    async def invoke_authorized(
        self,
        envelope: AuthorizedToolDispatchEnvelope,
        cancellation: CancellationSignal,
    ) -> ToolInvocationResult:
        run_id = self._runtime._require_run_id()
        if envelope.invocation.run_id != run_id:
            raise RunConfigurationError("REAL tool request belongs to another Run")
        registry = self._runtime._registry
        if registry is None:
            raise RunConfigurationError("REAL capability registry is unavailable")
        tool = registry.resolve(self._descriptor.capability_id)
        return await tool.invoke_authorized(envelope, cancellation)
