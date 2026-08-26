"""Deterministic in-memory lifecycle adapters."""

from __future__ import annotations

from threading import RLock

from researchos.application.errors import (
    CorruptRunState,
    RevisionConflict,
    RunAlreadyExists,
    RunNotFound,
)
from researchos.domain.contracts import RunState, TraceEvent, model_sha256
from researchos.security.redaction import PersistenceRedactor


class InMemoryRunStore:
    def __init__(self) -> None:
        self._states: dict[str, RunState] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, state: RunState) -> None:
        self._validate_state(state)
        with self._lock:
            if state.run_id in self._states:
                raise RunAlreadyExists(state.run_id)
            self._states[state.run_id] = state.model_copy(deep=True)

    def load(self, run_id: str) -> RunState:
        with self._lock:
            try:
                state = self._states[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise RunNotFound(run_id) from exc
            self._validate_state(state)
            return state

    def save(self, state: RunState, *, expected_revision: int) -> None:
        self._validate_state(state)
        with self._lock:
            try:
                current = self._states[state.run_id]
            except KeyError as exc:
                raise RunNotFound(state.run_id) from exc
            if current.revision != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            if state.revision != expected_revision + 1:
                raise RevisionConflict("new revision must increase by exactly one")
            self._states[state.run_id] = state.model_copy(deep=True)

    def _validate_state(self, state: RunState) -> None:
        self._redactor.assert_safe_model(state)
        if state.input_hash != model_sha256(state.input_snapshot):
            raise CorruptRunState("run state input hash does not match snapshot")
        if state.config_hash != model_sha256(state.config):
            raise CorruptRunState("run state config hash does not match snapshot")


class InMemoryTraceSink:
    def __init__(self) -> None:
        self._events: dict[str, list[TraceEvent]] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def append(self, event: TraceEvent) -> None:
        self._redactor.assert_safe_model(event)
        with self._lock:
            copied = event.model_copy(deep=True)
            self._events.setdefault(event.run_id, []).append(copied)

    def read(
        self, run_id: str, *, recover_torn_tail: bool = False
    ) -> tuple[TraceEvent, ...]:
        del recover_torn_tail
        with self._lock:
            return tuple(
                event.model_copy(deep=True) for event in self._events.get(run_id, [])
            )
