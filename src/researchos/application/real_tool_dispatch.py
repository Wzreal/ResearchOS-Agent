"""Immediate, read-only REAL Tool dispatch authorization."""

from __future__ import annotations

from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.errors import RunConfigurationError
from researchos.application.real_composition import (
    BoundRealCapability,
    RealCompositionManager,
)
from researchos.application.runtime_transitions import stable_key
from researchos.domain.contracts import OperatingMode, RunStatus, model_sha256
from researchos.domain.real_tools import AuthorizedToolDispatchEnvelope
from researchos.interfaces.lifecycle import RunStore


class RealToolDispatchAuthorizer:
    def __init__(
        self,
        *,
        runs: RunStore,
        compositions: RealCompositionManager,
        registry: CapabilityRegistry,
        bound: BoundRealCapability,
    ) -> None:
        self._runs = runs
        self._compositions = compositions
        self._registry = registry
        self._bound = bound

    def authorize(self, envelope: AuthorizedToolDispatchEnvelope) -> None:
        context = envelope.dispatch_context
        invocation = envelope.invocation
        descriptor = self._bound.settings.descriptor()
        state = self._runs.load(invocation.run_id)
        if (
            invocation.run_id != self._bound.run_id
            or context.run_id != self._bound.run_id
            or state.run_id != self._bound.run_id
        ):
            raise RunConfigurationError("REAL Tool request belongs to another Run")
        if state.config.mode is not OperatingMode.REAL:
            raise RunConfigurationError("REAL Tool requires a REAL Run")
        if state.status is not RunStatus.RUNNING:
            raise RunConfigurationError("REAL Tool dispatch requires RUNNING Run")
        if context.task_id != invocation.task_id:
            raise RunConfigurationError("REAL Tool task identity differs")
        if context.task_contract_hash != model_sha256(context.task):
            raise RunConfigurationError("REAL Tool task authority hash differs")
        if not set(context.task.required_capability_ids).issubset(
            state.config.allowed_capability_ids
        ):
            raise RunConfigurationError(
                "REAL Tool task exceeds Run capability authority"
            )
        registered = self._registry.resolve_authorized(
            context.requested_capability_id,
            context.authorized_capability_ids,
        )
        if registered.descriptor != descriptor:
            raise RunConfigurationError("REAL Tool registry binding differs")
        if context.descriptor_hash != model_sha256(descriptor):
            raise RunConfigurationError("REAL Tool descriptor hash differs")
        if descriptor.input_type != invocation.input.input_type:
            raise RunConfigurationError("REAL Tool input contract differs")
        expected_operation_key = stable_key(
            {
                "task_operation_key": context.task_operation_key,
                "agent_step": invocation.agent_step,
                "capability_id": descriptor.capability_id,
                "tool_id": descriptor.tool_id,
                "adapter_id": descriptor.adapter_id,
                "tool_operation_version": descriptor.operation_version,
                "safe_input_hash": model_sha256(invocation.input),
            }
        )
        if invocation.tool_operation_key != expected_operation_key:
            raise RunConfigurationError("REAL Tool operation identity differs")
        self._compositions.validate_tool_dispatch(
            state,
            composition_hash=self._bound.composition_hash,
            capability_id=context.requested_capability_id,
            descriptor=descriptor,
            reservation_hash=(
                self._bound.settings.provider_reservation.reservation_hash
            ),
        )
