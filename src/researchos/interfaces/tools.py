"""Phase 4 Tool and artifact ports."""

from typing import Protocol, runtime_checkable

from researchos.domain.real_tools import AuthorizedToolDispatchEnvelope
from researchos.domain.tools import (
    ToolArtifact,
    ToolDescriptor,
    ToolInvocationRequest,
    ToolInvocationResult,
)
from researchos.interfaces.runtime import CancellationSignal


class Tool(Protocol):
    @property
    def descriptor(self) -> ToolDescriptor: ...

    async def invoke(
        self, request: ToolInvocationRequest, cancellation: CancellationSignal
    ) -> ToolInvocationResult: ...


@runtime_checkable
class AuthorizedRealTool(Protocol):
    @property
    def descriptor(self) -> ToolDescriptor: ...

    async def invoke_authorized(
        self,
        envelope: AuthorizedToolDispatchEnvelope,
        cancellation: CancellationSignal,
    ) -> ToolInvocationResult: ...


@runtime_checkable
class Phase3RetryDelegatingTool(Protocol):
    """Marks infrastructure failures whose retry is owned by Phase 3."""

    @property
    def delegates_retry_to_phase3(self) -> bool: ...


class ArtifactStore(Protocol):
    def store_bytes(
        self,
        *,
        run_id: str,
        relative_path: str,
        content: bytes,
        media_type: str,
        producer_tool_id: str,
        tool_operation_key: str,
    ) -> ToolArtifact: ...
