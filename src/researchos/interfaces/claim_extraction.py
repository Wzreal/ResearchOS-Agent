"""Ports for the Phase 10 claim-extraction journal and model."""

from typing import Protocol

from researchos.domain.claim_extraction import (
    ClaimExtractionModelResult,
    ClaimExtractionRequest,
)
from researchos.domain.claim_extraction_operation import ClaimExtractionOperation


class ClaimExtractionOperationStore(Protocol):
    def create(self, operation: ClaimExtractionOperation) -> None: ...

    def load(self, run_id: str, extraction_id: str) -> ClaimExtractionOperation: ...

    def save(
        self, operation: ClaimExtractionOperation, *, expected_revision: int
    ) -> None: ...


class ClaimExtractionModel(Protocol):
    @property
    def model_bundle_hash(self) -> str: ...

    def generate(
        self, request: ClaimExtractionRequest
    ) -> ClaimExtractionModelResult: ...
