from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from researchos.adapters.memory import InMemoryRunStore, InMemoryTraceSink
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import RunConfig, RunInput


class FrozenClock:
    def __init__(self, current: datetime | None = None) -> None:
        self.current = current or datetime(2026, 8, 26, 8, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> None:
        self.current += timedelta(**kwargs)


class SequentialIds:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, prefix: str) -> str:
        self.count += 1
        return f"{prefix}_{self.count}"


@pytest.fixture
def lifecycle() -> tuple[
    RunManager,
    InMemoryRunStore,
    InMemoryTraceSink,
    FrozenClock,
    RunInput,
    RunConfig,
]:
    store = InMemoryRunStore()
    trace = InMemoryTraceSink()
    clock = FrozenClock()
    manager = RunManager(
        store=store,
        trace_sink=trace,
        clock=clock,
        id_factory=SequentialIds(),
    )
    return manager, store, trace, clock, RunInput(query="test query"), RunConfig()
