"""Deterministic optional observation exporter used only in offline tests."""

from __future__ import annotations

import asyncio

from researchos.domain.observability import ObservationEnvelope


class MockObservationExporter:
    def __init__(
        self,
        *,
        exporter_id: str = "mock_observation_exporter",
        delay_seconds: float = 0,
        fail: bool = False,
    ) -> None:
        self.exporter_id = exporter_id
        self.delay_seconds = delay_seconds
        self.fail = fail
        self.calls = 0
        self.envelopes: list[ObservationEnvelope] = []

    async def export(self, envelope: ObservationEnvelope) -> None:
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("mock exporter failure")
        self.envelopes.append(envelope.model_copy(deep=True))
