"""In-memory idempotent Tool artifact store."""

import hashlib

from researchos.application.errors import ArtifactConflictError
from researchos.application.runtime_transitions import stable_key
from researchos.domain.tools import ToolArtifact


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], tuple[bytes, ToolArtifact]] = {}

    def store_bytes(
        self,
        *,
        run_id: str,
        relative_path: str,
        content: bytes,
        media_type: str,
        producer_tool_id: str,
        tool_operation_key: str,
    ) -> ToolArtifact:
        digest = hashlib.sha256(content).hexdigest()
        identity = stable_key(
            {"operation": tool_operation_key, "path": relative_path}
        )
        artifact_id = f"artifact_{identity[:32]}"
        artifact = ToolArtifact(
            artifact_id=artifact_id,
            media_type=media_type,
            relative_path=relative_path,
            sha256=digest,
            size_bytes=len(content),
            producer_tool_id=producer_tool_id,
            tool_operation_key=tool_operation_key,
        )
        key = (run_id, relative_path)
        existing = self._items.get(key)
        if existing is not None and existing[1] != artifact:
            raise ArtifactConflictError("artifact identity has different content")
        self._items[key] = (bytes(content), artifact)
        return artifact

    def read(self, run_id: str, relative_path: str) -> bytes:
        return self._items[(run_id, relative_path)][0]
