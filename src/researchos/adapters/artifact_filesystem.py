"""Atomic run-relative Tool artifact store."""

import hashlib
from pathlib import Path

from pydantic import TypeAdapter

from researchos.adapters._atomic_file import atomic_replace_bytes
from researchos.application.errors import ArtifactConflictError
from researchos.application.runtime_transitions import stable_key
from researchos.domain.contracts import SafeId
from researchos.domain.tools import ToolArtifact


class FilesystemArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

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
        TypeAdapter(SafeId).validate_python(run_id)
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
        run_root = (self._root / run_id).resolve()
        target = (run_root / relative_path).resolve()
        if not target.is_relative_to(run_root):
            raise ArtifactConflictError("artifact target escapes run root")
        if target.exists():
            if target.is_symlink() or target.read_bytes() != content:
                raise ArtifactConflictError("artifact identity has different content")
            return artifact
        atomic_replace_bytes(target, content, fault=lambda _stage: None)
        return artifact
