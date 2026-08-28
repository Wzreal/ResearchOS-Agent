"""Deterministic, default-deny capability registry."""

from researchos.application.errors import (
    CapabilityConfigurationError,
    ToolPermissionDenied,
    UnknownCapabilityError,
)
from researchos.domain.tools import AdapterMode
from researchos.interfaces.tools import Tool


class CapabilityRegistry:
    """Bind exactly one active Tool to each capability in Phase 4."""

    def __init__(self, *, allowed_modes: frozenset[AdapterMode]) -> None:
        self._allowed_modes = allowed_modes
        self._by_capability: dict[str, Tool] = {}
        self._tool_ids: set[str] = set()

    def register(self, tool: Tool) -> None:
        descriptor = tool.descriptor
        self.require_mode(descriptor.mode)
        if descriptor.capability_id in self._by_capability:
            raise CapabilityConfigurationError("duplicate capability registration")
        if descriptor.tool_id in self._tool_ids:
            raise CapabilityConfigurationError("duplicate tool registration")
        self._by_capability[descriptor.capability_id] = tool
        self._tool_ids.add(descriptor.tool_id)

    def require_mode(self, mode: AdapterMode) -> None:
        if mode not in self._allowed_modes:
            raise CapabilityConfigurationError(
                f"adapter mode {mode.value} is not enabled"
            )

    def resolve(self, capability_id: str) -> Tool:
        try:
            return self._by_capability[capability_id]
        except KeyError as exc:
            raise UnknownCapabilityError(capability_id) from exc

    def resolve_authorized(
        self, capability_id: str, authorized_capability_ids: tuple[str, ...]
    ) -> Tool:
        tool = self.resolve(capability_id)
        if capability_id not in authorized_capability_ids:
            raise ToolPermissionDenied(capability_id)
        return tool

    def descriptors(self):
        return tuple(
            self._by_capability[key].descriptor
            for key in sorted(self._by_capability)
        )
