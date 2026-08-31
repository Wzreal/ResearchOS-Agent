"""Narrow lifecycle interfaces used by RunManager."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from researchos.domain.contracts import RunState, TraceEvent
from researchos.domain.observability import AppendOnceResult
from researchos.domain.runtime import TraceEventDescriptor


class RunStore(Protocol):
    def create(self, state: RunState) -> None: ...

    def load(self, run_id: str) -> RunState: ...

    def save(self, state: RunState, *, expected_revision: int) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class TraceSink(Protocol):
    def append(self, event: TraceEvent) -> None: ...

    def append_once(self, descriptor: TraceEventDescriptor) -> AppendOnceResult: ...

    def read(
        self, run_id: str, *, recover_torn_tail: bool = False
    ) -> tuple[TraceEvent, ...]: ...
