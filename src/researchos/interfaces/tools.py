"""Phase 4 Tool and artifact ports."""

from typing import Protocol

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
