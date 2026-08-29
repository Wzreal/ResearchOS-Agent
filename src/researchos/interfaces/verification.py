"""Provider-independent Phase 6 model and artifact ports."""

from __future__ import annotations

from typing import Protocol

from researchos.domain.synthesis import (
    VerificationModelRequest,
    VerificationModelResponse,
    VerificationResult,
)
from researchos.interfaces.runtime import CancellationSignal


class VerificationModel(Protocol):
    @property
    def model_bundle_hash(self) -> str: ...

    async def invoke(
        self, request: VerificationModelRequest, cancellation: CancellationSignal
    ) -> VerificationModelResponse: ...


class VerificationArtifactStore(Protocol):
    def load(self, run_id: str) -> VerificationResult | None: ...

    def publish(
        self,
        result: VerificationResult,
        markdown: bytes,
        *,
        expected_prior_verification_id: str | None,
    ) -> None: ...

    def reconcile_report(self, run_id: str) -> bool: ...
