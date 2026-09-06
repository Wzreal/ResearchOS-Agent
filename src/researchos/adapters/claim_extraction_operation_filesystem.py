"""Atomic filesystem persistence for the separate claim-extraction journal."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from researchos.adapters._atomic_file import AtomicWriteFailure, atomic_replace_bytes
from researchos.application.errors import (
    ClaimExtractionOperationAlreadyExists,
    ClaimExtractionOperationNotFound,
    ClaimExtractionOperationRevisionConflict,
    CorruptClaimExtractionOperation,
)
from researchos.domain.claim_extraction_operation import (
    ClaimExtractionOperation,
    ClaimExtractionOperationEnvelope,
)
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.security.redaction import PersistenceRedactor

FaultInjector = Callable[[str], None]


def _no_fault(_: str) -> None:
    return None


class FilesystemClaimExtractionOperationStore:
    directory_name = "claim_extraction_operations"

    def __init__(
        self, root: str | Path, *, fault_injector: FaultInjector | None = None
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._fault = fault_injector or _no_fault
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, operation: ClaimExtractionOperation) -> None:
        self._validate(operation)
        with self._lock:
            path = self._path(operation.run_id, operation.extraction_id)
            if path.exists():
                raise ClaimExtractionOperationAlreadyExists(operation.extraction_id)
            if operation.checkpoint_revision != 0:
                raise ClaimExtractionOperationRevisionConflict(
                    "initial operation revision must be zero"
                )
            self._write(path, operation)

    def load(self, run_id: str, extraction_id: str) -> ClaimExtractionOperation:
        with self._lock:
            path = self._path(run_id, extraction_id)
            if not path.exists():
                raise ClaimExtractionOperationNotFound(extraction_id)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                envelope = ClaimExtractionOperationEnvelope.model_validate(raw)
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValidationError,
            ) as exc:
                raise CorruptClaimExtractionOperation(
                    "operation file is invalid"
                ) from exc
            operation = envelope.operation.model_copy(
                update={"provider_diagnostics": envelope.provider_diagnostics}
            )
            if operation.run_id != run_id or operation.extraction_id != extraction_id:
                raise CorruptClaimExtractionOperation("operation identity differs")
            self._validate(operation)
            return operation

    def save(
        self, operation: ClaimExtractionOperation, *, expected_revision: int
    ) -> None:
        self._validate(operation)
        with self._lock:
            current = self.load(operation.run_id, operation.extraction_id)
            if current.operation_id != operation.operation_id:
                raise ClaimExtractionOperationRevisionConflict(
                    "operation identity changed"
                )
            if current.checkpoint_revision != expected_revision:
                raise ClaimExtractionOperationRevisionConflict(
                    "operation revision differs"
                )
            if operation.checkpoint_revision != expected_revision + 1:
                raise ClaimExtractionOperationRevisionConflict(
                    "operation revision must increment once"
                )
            self._write(
                self._path(operation.run_id, operation.extraction_id), operation
            )

    def _write(self, path: Path, operation: ClaimExtractionOperation) -> None:
        envelope = ClaimExtractionOperationEnvelope(
            operation=operation,
            payload_sha256=model_sha256(operation),
            provider_diagnostics=operation.provider_diagnostics,
        )
        try:
            atomic_replace_bytes(
                path, canonical_json_bytes(envelope) + b"\n", fault=self._fault
            )
        except AtomicWriteFailure as exc:
            raise CorruptClaimExtractionOperation(
                "operation atomic write failed"
            ) from exc

    def _validate(self, operation: ClaimExtractionOperation) -> None:
        self._redactor.assert_safe_model(operation)
        if operation.provider_diagnostics is not None:
            self._redactor.assert_safe_model(
                ClaimExtractionOperationEnvelope(
                    operation=operation,
                    payload_sha256=model_sha256(operation),
                    provider_diagnostics=operation.provider_diagnostics,
                )
            )
        try:
            validated = ClaimExtractionOperation.model_validate(
                operation.model_dump(mode="python")
            )
        except ValueError as exc:
            raise CorruptClaimExtractionOperation(
                "operation contract is invalid"
            ) from exc
        if validated != operation.model_copy(update={"provider_diagnostics": None}):
            raise CorruptClaimExtractionOperation("operation canonical form differs")

    def _path(self, run_id: str, extraction_id: str) -> Path:
        safe = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if any(
            not value or any(char not in safe for char in value)
            for value in (run_id, extraction_id)
        ):
            raise ValueError("unsafe operation path identity")
        run_dir = (self.root / run_id).resolve()
        if run_dir.parent != self.root:
            raise ValueError("operation path escapes output root")
        return run_dir / self.directory_name / f"{extraction_id}.json"
