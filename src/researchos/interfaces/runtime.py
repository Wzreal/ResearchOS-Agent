"""Narrow Phase 3 runtime ports."""

from __future__ import annotations

from typing import Protocol

from researchos.domain.runtime import (
    RuntimeCheckpoint,
    TaskExecutionRequest,
    TaskExecutionResult,
)


class CheckpointStore(Protocol):
    def create(self, checkpoint: RuntimeCheckpoint) -> None: ...

    def load(self, run_id: str) -> RuntimeCheckpoint: ...

    def save(
        self, checkpoint: RuntimeCheckpoint, *, expected_revision: int
    ) -> None: ...


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...

    async def wait(self) -> None: ...


class RunCancellationController(Protocol):
    """Run-level command boundary; attempt signals are derived views."""

    @property
    def cancellation_requested(self) -> bool: ...

    def request_cancel(self) -> None: ...

    def signal_for_attempt(self) -> CancellationSignal: ...


class TaskExecutionBackend(Protocol):
    async def execute(
        self, request: TaskExecutionRequest, cancellation: CancellationSignal
    ) -> TaskExecutionResult: ...


class AsyncSleeper(Protocol):
    async def sleep(self, milliseconds: int) -> None: ...
