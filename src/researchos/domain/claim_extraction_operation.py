"""Separate durable model-dispatch journal for Phase 10 claim extraction."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from researchos.domain.claim_extraction import ClaimExtractionResponse
from researchos.domain.contracts import (
    ContractModel,
    SafeId,
    Sha256,
    _require_aware,
    model_sha256,
)
from researchos.domain.provider_diagnostics import validate_provider_diagnostics
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty

CLAIM_EXTRACTION_OPERATION_SCHEMA_VERSION = 1


class ClaimExtractionOperationStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    VALIDATED = "validated"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED_UNKNOWN = "interrupted_unknown"


class ClaimExtractionOperation(ContractModel):
    schema_version: Literal[CLAIM_EXTRACTION_OPERATION_SCHEMA_VERSION] = (
        CLAIM_EXTRACTION_OPERATION_SCHEMA_VERSION
    )
    operation_id: SafeId
    checkpoint_revision: int = Field(default=0, ge=0)
    status: ClaimExtractionOperationStatus = ClaimExtractionOperationStatus.PREPARED
    run_id: SafeId
    run_revision: int = Field(ge=0)
    extraction_id: SafeId
    policy_hash: Sha256
    model_bundle_hash: Sha256
    evidence_snapshot_hash: Sha256
    request_hash: Sha256
    workflow_budget_slice_hash: Sha256
    validated_response: ClaimExtractionResponse | None = None
    usage: RuntimeResourceAmount | None = None
    usage_certainty: UsageCertainty = UsageCertainty.UNKNOWN
    mutation_keys: tuple[Sha256, ...] = ()
    failure_code: SafeId | None = None
    provider_diagnostics: dict[str, Any] | None = Field(default=None, exclude=True)
    created_at: datetime
    updated_at: datetime

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)

    @field_validator("provider_diagnostics")
    @classmethod
    def provider_diagnostics_are_json(cls, value: dict[str, Any] | None):
        return validate_provider_diagnostics(value)

    @model_validator(mode="after")
    def state_matches_durable_payload(self) -> ClaimExtractionOperation:
        validated = self.status in {
            ClaimExtractionOperationStatus.VALIDATED,
            ClaimExtractionOperationStatus.COMPLETED,
        }
        if validated != (self.validated_response is not None):
            raise ValueError("validated response is required exactly after validation")
        if self.validated_response is not None and (
            self.validated_response.extraction_id != self.extraction_id
        ):
            raise ValueError("validated response extraction identity differs")
        if self.usage_certainty is UsageCertainty.UNKNOWN and self.usage is not None:
            raise ValueError("unknown usage cannot contain an amount")
        if self.usage_certainty is not UsageCertainty.UNKNOWN and self.usage is None:
            raise ValueError("known usage certainty requires an amount")
        terminal_failure = self.status in {
            ClaimExtractionOperationStatus.FAILED,
            ClaimExtractionOperationStatus.CANCELLED,
            ClaimExtractionOperationStatus.INTERRUPTED_UNKNOWN,
        }
        if terminal_failure != (self.failure_code is not None):
            raise ValueError("terminal extraction failure requires a failure code")
        if len(self.mutation_keys) != len(set(self.mutation_keys)):
            raise ValueError("claim extraction mutation keys must be unique")
        if self.updated_at < self.created_at:
            raise ValueError("operation updated_at precedes created_at")
        return self


class ClaimExtractionOperationEnvelope(ContractModel):
    envelope_version: Literal[1] = 1
    operation: ClaimExtractionOperation
    payload_sha256: Sha256
    provider_diagnostics: dict[str, Any] | None = None

    @field_validator("provider_diagnostics")
    @classmethod
    def provider_diagnostics_are_json(cls, value: dict[str, Any] | None):
        return validate_provider_diagnostics(value)

    @model_validator(mode="after")
    def payload_hash_matches(self) -> ClaimExtractionOperationEnvelope:
        if self.payload_sha256 != model_sha256(self.operation):
            raise ValueError("claim extraction operation payload hash differs")
        return self
