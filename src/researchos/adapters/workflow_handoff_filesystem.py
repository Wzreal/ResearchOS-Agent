"""Atomic filesystem persistence for the Phase 10 workflow handoff."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from researchos.adapters._atomic_file import AtomicWriteFailure, atomic_replace_bytes
from researchos.application.errors import (
    CorruptWorkflowHandoff,
    WorkflowHandoffAlreadyExists,
    WorkflowHandoffNotFound,
    WorkflowHandoffPersistenceError,
    WorkflowHandoffRevisionConflict,
)
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.domain.workflow import (
    WorkflowRuntimeHandoff,
    WorkflowRuntimeHandoffEnvelope,
)
from researchos.security.redaction import PersistenceRedactor

FaultInjector = Callable[[str], None]


def _no_fault(_: str) -> None:
    return None


class FilesystemWorkflowRuntimeHandoffStore:
    filename = "workflow_runtime_handoff.json"

    def __init__(
        self, root: str | Path, *, fault_injector: FaultInjector | None = None
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._fault = fault_injector or _no_fault
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, handoff: WorkflowRuntimeHandoff) -> None:
        self._validate(handoff)
        with self._lock:
            path = self._path(handoff.run_id)
            if path.exists():
                raise WorkflowHandoffAlreadyExists(handoff.run_id)
            if handoff.handoff_revision != 0:
                raise WorkflowHandoffRevisionConflict(
                    "initial handoff revision must be zero"
                )
            self._write(path, handoff)

    def load(self, run_id: str) -> WorkflowRuntimeHandoff:
        with self._lock:
            path = self._path(run_id)
            if not path.exists():
                raise WorkflowHandoffNotFound(run_id)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                envelope = WorkflowRuntimeHandoffEnvelope.model_validate(raw)
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValidationError,
            ) as exc:
                raise CorruptWorkflowHandoff(
                    "workflow handoff file is invalid"
                ) from exc
            handoff = envelope.handoff
            if handoff.run_id != run_id:
                raise CorruptWorkflowHandoff("workflow handoff belongs to another run")
            self._validate(handoff)
            return handoff

    def save(self, handoff: WorkflowRuntimeHandoff, *, expected_revision: int) -> None:
        self._validate(handoff)
        with self._lock:
            current = self.load(handoff.run_id)
            if current.handoff_revision != expected_revision:
                raise WorkflowHandoffRevisionConflict("handoff revision differs")
            if current.handoff_id != handoff.handoff_id:
                raise WorkflowHandoffRevisionConflict("handoff identity changed")
            if handoff.handoff_revision != expected_revision + 1:
                raise WorkflowHandoffRevisionConflict(
                    "handoff revision must increment once"
                )
            self._write(self._path(handoff.run_id), handoff)

    def _write(self, path: Path, handoff: WorkflowRuntimeHandoff) -> None:
        envelope = WorkflowRuntimeHandoffEnvelope(
            handoff=handoff, payload_sha256=model_sha256(handoff)
        )
        try:
            atomic_replace_bytes(
                path, canonical_json_bytes(envelope) + b"\n", fault=self._fault
            )
        except AtomicWriteFailure as exc:
            raise WorkflowHandoffPersistenceError(
                "atomic workflow handoff write failed",
                run_id=handoff.run_id,
                handoff_replaced=exc.replaced,
            ) from exc

    def _validate(self, handoff: WorkflowRuntimeHandoff) -> None:
        self._redactor.assert_safe_model(handoff)
        try:
            validated = WorkflowRuntimeHandoff.model_validate(
                handoff.model_dump(mode="python")
            )
        except ValueError as exc:
            raise CorruptWorkflowHandoff("workflow handoff is invalid") from exc
        if validated != handoff:
            raise CorruptWorkflowHandoff("workflow handoff canonical form differs")

    def _path(self, run_id: str) -> Path:
        safe = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not run_id or any(char not in safe for char in run_id):
            raise ValueError("unsafe run ID")
        run_dir = (self.root / run_id).resolve()
        if run_dir.parent != self.root:
            raise ValueError("workflow handoff path escapes output root")
        return run_dir / self.filename
