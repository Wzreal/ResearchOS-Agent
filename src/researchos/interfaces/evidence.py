"""Phase 5 evidence and claim-graph persistence and ingestion ports."""

from __future__ import annotations

from typing import Protocol

from researchos.domain.agent import AgentContext, AgentObservation
from researchos.domain.claims import ClaimGraphSnapshot
from researchos.domain.evidence import EvidenceIngestionResult, EvidenceStoreSnapshot


class EvidenceStore(Protocol):
    def create(self, snapshot: EvidenceStoreSnapshot) -> None: ...
    def load(self, run_id: str) -> EvidenceStoreSnapshot: ...
    def save(
        self, snapshot: EvidenceStoreSnapshot, *, expected_revision: int
    ) -> None: ...


class ClaimGraphStore(Protocol):
    def create(self, snapshot: ClaimGraphSnapshot) -> None: ...
    def load(self, run_id: str) -> ClaimGraphSnapshot: ...
    def save(self, snapshot: ClaimGraphSnapshot, *, expected_revision: int) -> None: ...


class EvidenceObservationIngestor(Protocol):
    async def ingest_observation(
        self, context: AgentContext, observation: AgentObservation
    ) -> EvidenceIngestionResult: ...
