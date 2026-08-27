"""Async timing and explicit run cancellation adapters."""

from __future__ import annotations

import asyncio


class AsyncioSleeper:
    async def sleep(self, milliseconds: int) -> None:
        await asyncio.sleep(milliseconds / 1_000)


class _EventCancellationSignal:
    def __init__(self, event: asyncio.Event) -> None:
        self._event = event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class AsyncioRunCancellationController:
    """Run command source; each attempt receives a read-only signal view."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancellation_requested(self) -> bool:
        return self._event.is_set()

    def request_cancel(self) -> None:
        self._event.set()

    def signal_for_attempt(self) -> _EventCancellationSignal:
        return _EventCancellationSignal(self._event)


class ControlledSleeper:
    """Deterministic test sleeper which records but does not delay."""

    def __init__(self) -> None:
        self.delays: list[int] = []

    async def sleep(self, milliseconds: int) -> None:
        self.delays.append(milliseconds)
        await asyncio.sleep(0)
