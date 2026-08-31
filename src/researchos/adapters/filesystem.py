"""Atomic local filesystem storage for Phase 1 run lifecycle state."""

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
    CorruptRunState,
    IncompatibleSchema,
    ObservationCorruption,
    RevisionConflict,
    RunAlreadyExists,
    RunNotFound,
    StatePersistenceError,
    UnsafePersistenceData,
)
from researchos.domain.contracts import (
    SCHEMA_VERSION,
    RunState,
    TraceEvent,
    canonical_json_bytes,
    model_sha256,
)
from researchos.domain.observability import AppendOnceResult
from researchos.domain.runtime import TraceEventDescriptor
from researchos.security.redaction import PersistenceRedactor

FaultInjector = Callable[[str], None]
_TRACE_LOCKS_GUARD = RLock()
_TRACE_PATH_LOCKS: dict[str, tuple[RLock, int]] = {}


@contextmanager
def _trace_path_lock(path: Path) -> Iterator[None]:
    # Windows ``Path.resolve`` changes to a ``\\?\`` spelling after creation;
    # use one lexical absolute spelling so pre/post-create callers share a lock.
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _TRACE_LOCKS_GUARD:
        lock, users = _TRACE_PATH_LOCKS.get(key, (RLock(), 0))
        _TRACE_PATH_LOCKS[key] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        with _TRACE_LOCKS_GUARD:
            lock.release()
            current_lock, current_users = _TRACE_PATH_LOCKS[key]
            if current_users == 1:
                del _TRACE_PATH_LOCKS[key]
            else:
                _TRACE_PATH_LOCKS[key] = (current_lock, current_users - 1)


def _no_fault(_: str) -> None:
    return None


class _FilesystemBase:
    def __init__(
        self, root: str | Path, *, fault_injector: FaultInjector | None = None
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._fault = fault_injector or _no_fault
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def _run_dir(self, run_id: str) -> Path:
        safe_characters = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not run_id or any(char not in safe_characters for char in run_id):
            raise ValueError("unsafe run ID")
        result = (self.root / run_id).resolve()
        if result.parent != self.root:
            raise ValueError("run path escapes output root")
        return result

class FilesystemRunStore(_FilesystemBase):
    state_filename = "run_state.json"

    def create(self, state: RunState) -> None:
        self._redactor.assert_safe_model(state)
        with self._lock:
            path = self._run_dir(state.run_id) / self.state_filename
            if path.exists():
                raise RunAlreadyExists(state.run_id)
            self._atomic_write(
                path,
                canonical_json_bytes(state) + b"\n",
                run_id=state.run_id,
            )

    def load(self, run_id: str) -> RunState:
        with self._lock:
            path = self._run_dir(run_id) / self.state_filename
            if not path.exists():
                raise RunNotFound(run_id)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CorruptRunState(f"invalid state file for {run_id}") from exc
            if not isinstance(raw, dict):
                raise CorruptRunState("run state root must be a JSON object")
            if raw.get("schema_version") != SCHEMA_VERSION:
                raise IncompatibleSchema(
                    f"unsupported run state schema {raw.get('schema_version')!r}"
                )
            try:
                state = RunState.model_validate(raw)
            except ValidationError as exc:
                raise CorruptRunState(f"invalid state schema for {run_id}") from exc
            if state.run_id != run_id:
                raise CorruptRunState("state run ID does not match directory")
            self._redactor.assert_safe_model(state)
            if state.input_hash != model_sha256(state.input_snapshot):
                raise CorruptRunState("run state input hash does not match snapshot")
            if state.config_hash != model_sha256(state.config):
                raise CorruptRunState("run state config hash does not match snapshot")
            return state

    def save(self, state: RunState, *, expected_revision: int) -> None:
        self._redactor.assert_safe_model(state)
        with self._lock:
            current = self.load(state.run_id)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            if state.revision != expected_revision + 1:
                raise RevisionConflict("new revision must increase by exactly one")
            path = self._run_dir(state.run_id) / self.state_filename
            self._atomic_write(
                path,
                canonical_json_bytes(state) + b"\n",
                run_id=state.run_id,
            )

    def _atomic_write(self, path: Path, data: bytes, *, run_id: str) -> None:
        try:
            atomic_replace_bytes(path, data, fault=self._fault)
        except AtomicWriteFailure as exc:
            raise StatePersistenceError(
                "atomic state write failed",
                run_id=run_id,
                state_replaced=exc.replaced,
            ) from exc


class FilesystemTraceSink(_FilesystemBase):
    trace_filename = "trace.jsonl"

    def append(self, event: TraceEvent) -> None:
        self._redactor.assert_safe_model(event)
        with self._lock:
            run_dir = self._run_dir(event.run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            path = run_dir / self.trace_filename
            with _trace_path_lock(path):
                self._read_path(
                    path,
                    expected_run_id=event.run_id,
                    recover_torn_tail=True,
                )
                self._append_event_unlocked(path, event)

    def append_once(self, descriptor: TraceEventDescriptor) -> AppendOnceResult:
        event = descriptor.to_event()
        self._redactor.assert_safe_model(event)
        with self._lock:
            path = self._run_dir(event.run_id) / self.trace_filename
            with _trace_path_lock(path):
                events = self._read_path(
                    path, expected_run_id=event.run_id, recover_torn_tail=True
                )
                matches = [item for item in events if item.event_id == event.event_id]
                if len(matches) > 1:
                    raise ObservationCorruption(
                        "trace contains duplicate physical event identity"
                    )
                if matches:
                    if model_sha256(matches[0]) != descriptor.canonical_event_hash:
                        raise ObservationCorruption(
                            "trace event identity has different canonical content"
                        )
                    return AppendOnceResult.ALREADY_PRESENT
                path.parent.mkdir(parents=True, exist_ok=True)
                self._append_event_unlocked(path, event)
                return AppendOnceResult.APPENDED

    def _append_event_unlocked(self, path: Path, event: TraceEvent) -> None:
        data = canonical_json_bytes(event) + b"\n"
        self._fault("before_trace_append")
        with path.open("ab") as handle:
            handle.write(data)
            self._fault("after_trace_write")
            handle.flush()
            self._fault("after_trace_flush")
            os.fsync(handle.fileno())
            self._fault("after_trace_fsync")

    def read(
        self, run_id: str, *, recover_torn_tail: bool = False
    ) -> tuple[TraceEvent, ...]:
        with self._lock:
            path = self._run_dir(run_id) / self.trace_filename
            with _trace_path_lock(path):
                return self._read_path(
                    path,
                    expected_run_id=run_id,
                    recover_torn_tail=recover_torn_tail,
                )

    def _read_path(
        self,
        path: Path,
        *,
        expected_run_id: str,
        recover_torn_tail: bool,
    ) -> tuple[TraceEvent, ...]:
        if not path.exists():
            return ()
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CorruptRunState("trace file cannot be read") from exc
        if data and not data.endswith(b"\n"):
            last_newline = data.rfind(b"\n")
            complete_end = last_newline + 1
            complete = data[:complete_end]
            self._parse_complete_lines(complete, expected_run_id=expected_run_id)
            if not recover_torn_tail:
                raise CorruptRunState("trace has a torn final event")
            self._fault("before_trace_tail_truncate")
            with path.open("r+b") as handle:
                handle.truncate(complete_end)
                handle.flush()
                os.fsync(handle.fileno())
            self._fault("after_trace_tail_fsync")
            data = complete
        return self._parse_complete_lines(data, expected_run_id=expected_run_id)

    def _parse_complete_lines(
        self, data: bytes, *, expected_run_id: str
    ) -> tuple[TraceEvent, ...]:
        events: list[TraceEvent] = []
        for line_number, line in enumerate(data.splitlines(), start=1):
            if not line:
                raise CorruptRunState(f"blank trace line {line_number}")
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise CorruptRunState(
                        f"trace line {line_number} must be a JSON object"
                    )
                if raw.get("schema_version") != SCHEMA_VERSION:
                    raise IncompatibleSchema(
                        f"unsupported trace schema {raw.get('schema_version')!r}"
                    )
                event = TraceEvent.model_validate(raw)
                if event.run_id != expected_run_id:
                    raise CorruptRunState(
                        f"trace line {line_number} belongs to run {event.run_id}, "
                        f"expected {expected_run_id}"
                    )
                try:
                    self._redactor.assert_safe_model(event)
                except UnsafePersistenceData as exc:
                    raise CorruptRunState(
                        f"trace line {line_number} contains unsafe persisted data"
                    ) from exc
                events.append(event)
            except (CorruptRunState, IncompatibleSchema):
                raise
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                raise CorruptRunState(f"invalid trace line {line_number}") from exc
        return tuple(events)
