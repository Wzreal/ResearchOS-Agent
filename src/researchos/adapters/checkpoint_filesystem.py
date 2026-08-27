"""Atomic filesystem checkpoint persistence for Phase 3."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from researchos.adapters._atomic_file import AtomicWriteFailure, atomic_replace_bytes
from researchos.application.errors import (
    CheckpointAlreadyExists,
    CheckpointCompatibilityError,
    CheckpointNotFound,
    CheckpointPersistenceError,
    CheckpointRevisionConflict,
    CorruptCheckpoint,
    UnsafePersistenceData,
)
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.domain.runtime import (
    RUNTIME_SCHEMA_VERSION,
    CheckpointEnvelope,
    RuntimeCheckpoint,
)
from researchos.security.redaction import PersistenceRedactor

FaultInjector = Callable[[str], None]


def _no_fault(_: str) -> None:
    return None


class FilesystemCheckpointStore:
    checkpoint_filename = "checkpoint.json"

    def __init__(
        self, root: str | Path, *, fault_injector: FaultInjector | None = None
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._fault = fault_injector or _no_fault
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, checkpoint: RuntimeCheckpoint) -> None:
        self._validate(checkpoint)
        with self._lock:
            path = self._path(checkpoint.run_id)
            if path.exists():
                raise CheckpointAlreadyExists(checkpoint.run_id)
            self._write(path, checkpoint)

    def load(self, run_id: str) -> RuntimeCheckpoint:
        with self._lock:
            path = self._path(run_id)
            if not path.exists():
                raise CheckpointNotFound(run_id)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CorruptCheckpoint("checkpoint file is invalid JSON") from exc
            if not isinstance(raw, dict):
                raise CorruptCheckpoint("checkpoint envelope must be an object")
            checkpoint_raw = raw.get("checkpoint")
            if (
                not isinstance(checkpoint_raw, dict)
                or checkpoint_raw.get("schema_version") != RUNTIME_SCHEMA_VERSION
            ):
                raise CheckpointCompatibilityError(
                    "unsupported runtime checkpoint schema"
                )
            try:
                envelope = CheckpointEnvelope.model_validate(raw)
            except ValidationError as exc:
                raise CorruptCheckpoint("checkpoint schema or hash is invalid") from exc
            checkpoint = envelope.checkpoint
            if checkpoint.run_id != run_id:
                raise CorruptCheckpoint("checkpoint belongs to another run")
            try:
                self._validate(checkpoint)
            except UnsafePersistenceData as exc:
                raise CorruptCheckpoint(
                    "checkpoint contains unsafe persisted data"
                ) from exc
            return checkpoint

    def save(self, checkpoint: RuntimeCheckpoint, *, expected_revision: int) -> None:
        self._validate(checkpoint)
        with self._lock:
            current = self.load(checkpoint.run_id)
            if current.checkpoint_revision != expected_revision:
                raise CheckpointRevisionConflict(
                    f"expected checkpoint revision {expected_revision}, "
                    f"found {current.checkpoint_revision}"
                )
            if checkpoint.checkpoint_revision != expected_revision + 1:
                raise CheckpointRevisionConflict(
                    "checkpoint revision must increase by exactly one"
                )
            self._write(self._path(checkpoint.run_id), checkpoint)

    def _write(self, path: Path, checkpoint: RuntimeCheckpoint) -> None:
        envelope = CheckpointEnvelope(
            checkpoint=checkpoint, payload_sha256=model_sha256(checkpoint)
        )
        try:
            atomic_replace_bytes(
                path, canonical_json_bytes(envelope) + b"\n", fault=self._fault
            )
        except AtomicWriteFailure as exc:
            raise CheckpointPersistenceError(
                "atomic checkpoint write failed",
                run_id=checkpoint.run_id,
                checkpoint_revision=checkpoint.checkpoint_revision,
                checkpoint_replaced=exc.replaced,
            ) from exc

    def _validate(self, checkpoint: RuntimeCheckpoint) -> None:
        # Deliberately reject instead of redacting; changing the DAG would make
        # both the graph semantics and its canonical hash dishonest.
        self._redactor.assert_safe_model(checkpoint)
        if checkpoint.dag_hash != model_sha256(checkpoint.dag):
            raise CorruptCheckpoint("checkpoint DAG hash differs")
        if checkpoint.execution_policy_hash != model_sha256(
            checkpoint.execution_policy
        ):
            raise CorruptCheckpoint("checkpoint policy hash differs")

    def _path(self, run_id: str) -> Path:
        safe = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not run_id or any(char not in safe for char in run_id):
            raise ValueError("unsafe run ID")
        run_dir = (self.root / run_id).resolve()
        if run_dir.parent != self.root:
            raise ValueError("checkpoint path escapes output root")
        return run_dir / self.checkpoint_filename
