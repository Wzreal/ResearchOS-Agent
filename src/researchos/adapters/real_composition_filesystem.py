"""Atomic immutable REAL composition filesystem authority."""

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
    RealCompositionConflict,
    RealCompositionCorruption,
    RealCompositionNotFound,
    RealCompositionPersistenceError,
    UnsafePersistenceData,
)
from researchos.domain.contracts import canonical_json_bytes
from researchos.domain.real_composition import (
    MAX_REAL_COMPOSITION_BYTES,
    RealCompositionEnvelope,
    RealCompositionSnapshot,
)
from researchos.interfaces.lifecycle import Clock
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
            current_lock, users = _PATH_LOCKS[key]
            if users == 1:
                del _PATH_LOCKS[key]
            else:
                _PATH_LOCKS[key] = (current_lock, users - 1)


class FilesystemRealCompositionStore:
    def __init__(
        self,
        root: str | Path,
        *,
        clock: Clock,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._fault = fault_injector or (lambda _stage: None)
        self._redactor = PersistenceRedactor()

    def create(self, snapshot: RealCompositionSnapshot) -> RealCompositionEnvelope:
        self._validate_snapshot(snapshot)
        path = self._path(snapshot.semantic.run_id)
        with _path_lock(path):
            if path.exists():
                existing = self._load_unlocked(path, snapshot.semantic.run_id)
                if existing.snapshot == snapshot:
                    return existing
                raise RealCompositionConflict("REAL composition already differs")
            envelope = RealCompositionEnvelope.build(
                snapshot, recorded_at=self._clock.now()
            )
            self._redactor.assert_safe_model(envelope)
            encoded = canonical_json_bytes(envelope) + b"\n"
            if len(encoded) > MAX_REAL_COMPOSITION_BYTES:
                raise RealCompositionCorruption("REAL composition exceeds byte limit")
            try:
                atomic_replace_bytes(path, encoded, fault=self._fault)
            except AtomicWriteFailure as exc:
                raise RealCompositionPersistenceError(
                    "REAL composition atomic write failed",
                    run_id=snapshot.semantic.run_id,
                    authority_replaced=exc.replaced,
                ) from exc
            return envelope

    def load(self, run_id: str) -> RealCompositionEnvelope:
        path = self._path(run_id)
        with _path_lock(path):
            return self._load_unlocked(path, run_id)

    def _load_unlocked(self, path: Path, run_id: str) -> RealCompositionEnvelope:
        if not path.exists():
            raise RealCompositionNotFound(run_id)
        try:
            encoded = path.read_bytes()
            if len(encoded) > MAX_REAL_COMPOSITION_BYTES:
                raise RealCompositionCorruption(
                    "REAL composition exceeds byte limit"
                )
            raw = json.loads(encoded)
            envelope = RealCompositionEnvelope.model_validate(raw)
            self._redactor.assert_safe_model(envelope)
            if encoded != canonical_json_bytes(envelope) + b"\n":
                raise RealCompositionCorruption(
                    "REAL composition canonical bytes differ"
                )
        except RealCompositionCorruption:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            UnsafePersistenceData,
        ) as exc:
            raise RealCompositionCorruption(
                "REAL composition authority is invalid"
            ) from exc
        if envelope.snapshot.semantic.run_id != run_id:
            raise RealCompositionCorruption("REAL composition belongs to another run")
        return envelope

    def _validate_snapshot(self, snapshot: RealCompositionSnapshot) -> None:
        try:
            self._redactor.assert_safe_model(snapshot)
            RealCompositionSnapshot.model_validate(snapshot.model_dump(mode="python"))
        except (ValidationError, ValueError, UnsafePersistenceData) as exc:
            raise RealCompositionCorruption(
                "REAL composition snapshot is invalid"
            ) from exc

    def _path(self, run_id: str) -> Path:
        safe = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not run_id or any(character not in safe for character in run_id):
            raise ValueError("unsafe Run ID")
        run_root = (self._root / run_id).resolve()
        if run_root.parent != self._root:
            raise ValueError("REAL composition path escapes root")
        return run_root / "real_composition.json"
