"""Provider-independent Phase 6 model and artifact ports."""

from __future__ import annotations

from typing import Any, Protocol

from researchos.domain.observability import ObservationEnvelope
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.synthesis import (
    VerificationModelRequest,
    VerificationModelResponse,
    VerificationResult,
)
from researchos.domain.verification_runtime import (
    CommittedModelResponse,
    ModelCallStatus,
    PreparedModelCall,
    VerificationOperation,
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


class VerificationOperationStore(Protocol):
    def create(self, operation: VerificationOperation) -> None: ...

    def load(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperation: ...

    def list_for_run(self, run_id: str) -> tuple[VerificationOperation, ...]: ...

    def save(
        self, operation: VerificationOperation, *, expected_revision: int
    ) -> None: ...


class VerificationCallJournal(Protocol):
    def prepare(
        self, request: VerificationModelRequest, *, response_contract_version: str
    ) -> tuple[PreparedModelCall, CommittedModelResponse | None]: ...

    def mark_dispatched(self, prepared: PreparedModelCall) -> None: ...

    def commit_response(
        self,
        prepared: PreparedModelCall,
        *,
        validated_payload: dict[str, Any],
        response: VerificationModelResponse,
    ) -> CommittedModelResponse: ...

    def mark_terminal(
        self,
        prepared: PreparedModelCall,
        *,
        status: ModelCallStatus,
        failure_code: str,
        usage_certainty: UsageCertainty,
        usage: RuntimeResourceAmount | None = None,
    ) -> None: ...

    def mark_ready_to_publish(
        self,
        result: VerificationResult,
        markdown: bytes,
        *,
        expected_prior_verification_id: str | None,
    ) -> None: ...


class ObservationExporter(Protocol):
    exporter_id: str

    async def export(self, envelope: ObservationEnvelope) -> None: ...
