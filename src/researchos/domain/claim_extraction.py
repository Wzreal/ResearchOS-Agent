"""Strict typed Phase 10 claim-extraction contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.contracts import ContractModel, SafeId, Sha256, _require_aware
from researchos.domain.provider_diagnostics import validate_provider_diagnostics
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty

CLAIM_EXTRACTION_SCHEMA_VERSION = 1


class ClaimExtractionPolicy(ContractModel):
    schema_version: Literal[CLAIM_EXTRACTION_SCHEMA_VERSION] = (
        CLAIM_EXTRACTION_SCHEMA_VERSION
    )
    max_evidence_items: int = Field(ge=1, le=1_000)
    max_context_bytes: int = Field(ge=1, le=2_000_000)
    max_claims: int = Field(ge=1, le=1_000)
    max_references_per_claim: int = Field(ge=1, le=1_000)
    max_statement_bytes: int = Field(ge=1, le=100_000)
    max_response_bytes: int = Field(ge=1, le=2_000_000)


class ClaimExtractionEvidenceItem(ContractModel):
    evidence_id: SafeId
    revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=100_000)
    content_hash: Sha256

    @property
    def content_bytes(self) -> int:
        return len(self.content.encode("utf-8"))


class ClaimEvidencePin(ContractModel):
    evidence_id: SafeId
    evidence_revision: int = Field(ge=1)
    relation: ClaimEvidenceRelation


class ClaimCandidate(ContractModel):
    ordinal: int = Field(ge=1)
    statement: str = Field(min_length=1, max_length=20_000)
    evidence_pins: tuple[ClaimEvidencePin, ...]

    @model_validator(mode="after")
    def pins_are_unique_and_supported(self) -> ClaimCandidate:
        refs = [
            (item.evidence_id, item.evidence_revision) for item in self.evidence_pins
        ]
        if len(refs) != len(set(refs)):
            raise ValueError("claim candidate contains duplicate evidence references")
        if not any(
            item.relation is ClaimEvidenceRelation.SUPPORTS
            for item in self.evidence_pins
        ):
            raise ValueError("claim candidate requires a SUPPORTS evidence reference")
        return self


class ClaimExtractionRequest(ContractModel):
    schema_version: Literal[CLAIM_EXTRACTION_SCHEMA_VERSION] = (
        CLAIM_EXTRACTION_SCHEMA_VERSION
    )
    request_id: SafeId
    run_id: SafeId
    run_revision: int = Field(ge=0)
    extraction_id: SafeId
    policy: ClaimExtractionPolicy
    policy_hash: Sha256
    evidence_snapshot_hash: Sha256
    evidence_items: tuple[ClaimExtractionEvidenceItem, ...]
    omitted_evidence_ids: tuple[SafeId, ...] = ()
    requested_at: datetime

    _aware_requested = field_validator("requested_at")(_require_aware)

    @model_validator(mode="after")
    def whole_items_fit_policy(self) -> ClaimExtractionRequest:
        ids = [item.evidence_id for item in self.evidence_items]
        if len(ids) != len(set(ids)) or set(ids) & set(self.omitted_evidence_ids):
            raise ValueError("claim extraction evidence admission is ambiguous")
        if len(self.evidence_items) > self.policy.max_evidence_items:
            raise ValueError("claim extraction evidence item bound exceeded")
        if (
            sum(item.content_bytes for item in self.evidence_items)
            > self.policy.max_context_bytes
        ):
            raise ValueError("claim extraction context bound exceeded")
        return self


class ClaimExtractionResponse(ContractModel):
    schema_version: Literal[CLAIM_EXTRACTION_SCHEMA_VERSION] = (
        CLAIM_EXTRACTION_SCHEMA_VERSION
    )
    run_id: SafeId
    extraction_id: SafeId
    candidates: tuple[ClaimCandidate, ...]


    @model_validator(mode="after")
    def ordinals_are_unique_and_contiguous(self) -> ClaimExtractionResponse:
        ordinals = [item.ordinal for item in self.candidates]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("claim candidate ordinals must be contiguous")
        return self


class ClaimExtractionModelResult(ContractModel):
    """Semantic extraction output plus separately accounted provider settlement."""

    response: ClaimExtractionResponse
    usage: RuntimeResourceAmount | None = None
    usage_certainty: UsageCertainty = UsageCertainty.UNKNOWN
    provider_diagnostics: dict[str, Any] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def usage_matches_certainty(self) -> ClaimExtractionModelResult:
        if self.usage_certainty is UsageCertainty.UNKNOWN and self.usage is not None:
            raise ValueError("unknown usage cannot contain an amount")
        if self.usage_certainty is not UsageCertainty.UNKNOWN and self.usage is None:
            raise ValueError("known usage certainty requires an amount")
        return self

    @field_validator("provider_diagnostics")
    @classmethod
    def provider_diagnostics_must_be_json(cls, value: dict[str, Any] | None):
        return validate_provider_diagnostics(value)
