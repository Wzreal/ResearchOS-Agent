"""Atomic in-process CAS storage for Phase 8 verification operations."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from researchos.adapters._atomic_file import AtomicWriteFailure, atomic_replace_bytes
from researchos.application.errors import (
    CorruptVerificationOperation,
    UnsafePersistenceData,
    VerificationOperationAlreadyExists,
    VerificationOperationNotFound,
    VerificationOperationPersistenceError,
    VerificationOperationRevisionConflict,
)
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.domain.verification_runtime import (
    DEFAULT_MAX_OPERATION_CHECKPOINT_BYTES,
    MAX_OPERATION_CHECKPOINT_BYTES,
    VERIFICATION_RUNTIME_SCHEMA_VERSION,
    VerificationOperation,
    VerificationOperationEnvelope,
    VerificationOperationStatus,
)
from researchos.security.redaction import PersistenceRedactor

FaultInjector = Callable[[str], None]
_LOCKS_GUARD = RLock()
_PATH_LOCKS: dict[str, tuple[RLock, int]] = {}


@contextmanager
def _path_lock(path: Path) -> Iterator[None]:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _LOCKS_GUARD:
        lock, users = _PATH_LOCKS.get(key, (RLock(), 0))
        _PATH_LOCKS[key] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        with _LOCKS_GUARD:
            lock.release()
            current_lock, current_users = _PATH_LOCKS[key]
            if current_users == 1:
                del _PATH_LOCKS[key]
            else:
                _PATH_LOCKS[key] = (current_lock, current_users - 1)


class FilesystemVerificationOperationStore:
    def __init__(
        self,
        root: str | Path,
        *,
        fault_injector: FaultInjector | None = None,
        max_checkpoint_bytes: int = DEFAULT_MAX_OPERATION_CHECKPOINT_BYTES,
    ) -> None:
        if not 1 <= max_checkpoint_bytes <= MAX_OPERATION_CHECKPOINT_BYTES:
            raise ValueError("operation checkpoint byte limit is invalid")
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._fault = fault_injector or (lambda _stage: None)
        self._redactor = PersistenceRedactor()
        self._max_checkpoint_bytes = max_checkpoint_bytes

    def create(self, operation: VerificationOperation) -> None:
        self._validate(operation)
        if operation.checkpoint_revision != 0:
            raise VerificationOperationRevisionConflict(
                "initial operation revision must be zero"
            )
        path = self._path(operation.run_id, operation.verification_id)
        operations_root = self._operations_root(operation.run_id)
        with _path_lock(operations_root):
            if path.exists():
                raise VerificationOperationAlreadyExists(operation.run_id)
            existing = (
                tuple(
                    self._load_unlocked(item, operation.run_id)
                    for item in sorted(operations_root.glob("*.json"))
                )
                if operations_root.exists()
                else ()
            )
            self._assert_generation_create_allowed(operation, existing)
            self._write(path, operation)

    def load(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperation:
        path = self._path(run_id, verification_id)
        with _path_lock(path):
            return self._load_unlocked(path, run_id)

    def list_for_run(self, run_id: str) -> tuple[VerificationOperation, ...]:
        operations_root = self._operations_root(run_id)
        with _path_lock(operations_root):
            if not operations_root.exists():
                return ()
            paths = tuple(sorted(operations_root.glob("*.json")))
            return tuple(self._load_unlocked(path, run_id) for path in paths)

    def save(
        self, operation: VerificationOperation, *, expected_revision: int
    ) -> None:
        self._validate(operation)
        path = self._path(operation.run_id, operation.verification_id)
        # The reload and replace are deliberately inside one shared lock. This
        # is the actual in-process CAS boundary, including across adapter instances.
        with _path_lock(path):
            current = self._load_unlocked(path, operation.run_id)
            if current.operation_id != operation.operation_id:
                raise VerificationOperationRevisionConflict(
                    "verification operation identity changed"
                )
            if current.checkpoint_revision != expected_revision:
                raise VerificationOperationRevisionConflict(
                    f"expected revision {expected_revision}, "
                    f"found {current.checkpoint_revision}"
                )
            if operation.checkpoint_revision != expected_revision + 1:
                raise VerificationOperationRevisionConflict(
                    "operation revision must increase by exactly one"
                )
            self._write(path, operation)

    def _load_unlocked(self, path: Path, run_id: str) -> VerificationOperation:
        if not path.exists():
            raise VerificationOperationNotFound(run_id)
        try:
            encoded = path.read_bytes()
            if len(encoded) > self._max_checkpoint_bytes:
                raise CorruptVerificationOperation(
                    "verification operation exceeds checkpoint byte limit"
                )
            raw = json.loads(encoded)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptVerificationOperation(
                "verification operation is invalid JSON"
            ) from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != (
            VERIFICATION_RUNTIME_SCHEMA_VERSION
        ):
            raise CorruptVerificationOperation(
                "unsupported verification operation schema"
            )
        try:
            envelope = VerificationOperationEnvelope.model_validate(raw)
            self._validate(envelope.operation)
        except (ValidationError, ValueError, UnsafePersistenceData) as exc:
            raise CorruptVerificationOperation(
                "verification operation schema or hash is invalid"
            ) from exc
        if envelope.operation.run_id != run_id:
            raise CorruptVerificationOperation(
                "verification operation belongs to another run"
            )
        return envelope.operation

    def _write(self, path: Path, operation: VerificationOperation) -> None:
        envelope = VerificationOperationEnvelope(
            operation=operation, payload_sha256=model_sha256(operation)
        )
        encoded = canonical_json_bytes(envelope) + b"\n"
        if len(encoded) > self._max_checkpoint_bytes:
            raise CorruptVerificationOperation(
                "verification operation exceeds checkpoint byte limit"
            )
        try:
            atomic_replace_bytes(
                path,
                encoded,
                fault=self._fault,
            )
        except AtomicWriteFailure as exc:
            raise VerificationOperationPersistenceError(
                "verification operation atomic write failed",
                operation_id=operation.operation_id,
                checkpoint_revision=operation.checkpoint_revision,
                checkpoint_replaced=exc.replaced,
            ) from exc

    def _validate(self, operation: VerificationOperation) -> None:
        self._redactor.assert_safe_model(operation)
        try:
            validated = VerificationOperation.model_validate(
                operation.model_dump(mode="python")
            )
        except ValueError as exc:
            raise CorruptVerificationOperation(
                "verification operation contract is invalid"
            ) from exc
        if validated != operation:
            raise CorruptVerificationOperation(
                "verification operation canonical form differs"
            )

    @staticmethod
    def _assert_generation_create_allowed(
        operation: VerificationOperation,
        existing: tuple[VerificationOperation, ...],
    ) -> None:
        if not existing:
            return
        if operation.expected_prior_verification_id is None or any(
            item.status is not VerificationOperationStatus.COMPLETED
            for item in existing
        ):
            raise VerificationOperationAlreadyExists(
                "another verification generation is unresolved"
            )
        if operation.expected_prior_verification_id not in {
            item.verification_id for item in existing
        }:
            raise VerificationOperationAlreadyExists(
                "completed generations do not include declared predecessor"
            )

    def _path(self, run_id: str, verification_id: str | None) -> Path:
        operations_root = self._operations_root(run_id)
        if verification_id is None:
            if not operations_root.exists():
                raise VerificationOperationNotFound(run_id)
            matches = tuple(operations_root.glob("*.json"))
            if len(matches) != 1:
                raise VerificationOperationNotFound(run_id)
            return matches[0]
        safe = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not verification_id or any(char not in safe for char in verification_id):
            raise ValueError("unsafe verification ID")
        path = (operations_root / f"{verification_id}.json").resolve()
        if path.parent != operations_root:
            raise ValueError("verification operation path escapes generation root")
        return path

    def _operations_root(self, run_id: str) -> Path:
        safe = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not run_id or any(char not in safe for char in run_id):
            raise ValueError("unsafe run ID")
        run_root = (self._root / run_id).resolve()
        if run_root.parent != self._root:
            raise ValueError("verification operation path escapes root")
        operations_root = (run_root / "verification_operations").resolve()
        if operations_root.parent != run_root:
            raise ValueError("verification operation path escapes run root")
        return operations_root
