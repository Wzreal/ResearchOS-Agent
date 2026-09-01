"""Narrow lifecycle interfaces used by RunManager."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from researchos.domain.contracts import RunConfig, RunState, RunStatus, TraceEvent
from researchos.domain.observability import AppendOnceResult
from researchos.domain.real_composition import (
    RealCompositionEnvelope,
    RealCompositionSnapshot,
)
from researchos.domain.runtime import TraceEventDescriptor


class RunStore(Protocol):
    def create(self, state: RunState) -> None: ...

    def load(self, run_id: str) -> RunState: ...

    def save(self, state: RunState, *, expected_revision: int) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealCompositionStore(Protocol):
    def create(self, snapshot: RealCompositionSnapshot) -> RealCompositionEnvelope: ...

    def load(self, run_id: str) -> RealCompositionEnvelope: ...


class RunIntegrationGuard(Protocol):
    def validate_create(self, config: RunConfig) -> None: ...

    def bind_created(self, state: RunState) -> None: ...

    def validate_resume(self, state: RunState) -> None: ...

    def validate_bound_state(self, state: RunState) -> None: ...

    def validate_transition(self, state: RunState, target: RunStatus) -> None: ...


class TraceSink(Protocol):
    def append(self, event: TraceEvent) -> None: ...

    def append_once(self, descriptor: TraceEventDescriptor) -> AppendOnceResult: ...

    def read(
        self, run_id: str, *, recover_torn_tail: bool = False
    ) -> tuple[TraceEvent, ...]: ...
