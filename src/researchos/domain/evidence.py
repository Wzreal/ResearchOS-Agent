"""Phase 5 evidence, provenance, revision, and ingestion contracts."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.contracts import ContractModel, SafeId, Sha256, _require_aware
from researchos.domain.identity import (
    normalize_content,
    sha256_text,
    stable_hash,
    stable_id,
)

EVIDENCE_SCHEMA_VERSION = 1
Text = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
Locator = Annotated[str, StringConstraints(min_length=1, max_length=2_048)]


class SourceType(StrEnum):
    WEB = "web"
    LOCAL_DOCUMENT = "local_document"


class RecordLifecycle(StrEnum):
    ACTIVE = "active"
    TOMBSTONED = "tombstoned"


class EvidenceIngestionDisposition(StrEnum):
    CREATED = "created"
    DEDUPLICATED = "deduplicated"
    REVISED = "revised"


def _json_object(value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > 32_768:
        raise ValueError("metadata exceeds 32768 bytes")
    return value


class SourceRecord(ContractModel):
    schema_version: Literal[EVIDENCE_SCHEMA_VERSION] = EVIDENCE_SCHEMA_VERSION
    run_id: SafeId
    source_id: SafeId
    source_type: SourceType
    canonical_locator: Locator
    original_locator: Locator
    first_seen_at: datetime
    first_adapter_id: SafeId

    _aware = field_validator("first_seen_at")(_require_aware)


class EvidenceRecord(ContractModel):
    schema_version: Literal[EVIDENCE_SCHEMA_VERSION] = EVIDENCE_SCHEMA_VERSION
    run_id: SafeId
    evidence_id: SafeId
    source_id: SafeId
    evidence_scope_key: SafeId
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    current_revision: int = Field(ge=1)
    lifecycle: RecordLifecycle = RecordLifecycle.ACTIVE
    created_at: datetime
    updated_at: datetime

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)


class EvidenceRevision(ContractModel):
    schema_version: Literal[EVIDENCE_SCHEMA_VERSION] = EVIDENCE_SCHEMA_VERSION
    run_id: SafeId
    evidence_id: SafeId
    revision: int = Field(ge=1)
    content: Text
    content_hash: Sha256
    normalized_content_hash: Sha256
    extraction_context: dict[str, Any] = Field(default_factory=dict)
    extraction_context_hash: Sha256
    extractor_id: SafeId
    extractor_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    created_at: datetime
    supersedes_revision: int | None = Field(default=None, ge=1)

    _safe_context = field_validator("extraction_context")(_json_object)
    _aware = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def revision_chain_is_local(self) -> EvidenceRevision:
        expected = None if self.revision == 1 else self.revision - 1
        if self.supersedes_revision != expected:
            raise ValueError(
                "evidence revision must supersede its immediate predecessor"
            )
        return self


class EvidenceRevisionRef(ContractModel):
    evidence_id: SafeId
    revision: int = Field(ge=1)


class EvidenceView(ContractModel):
    evidence: EvidenceRecord
    revision: EvidenceRevision


class IngestionReceipt(ContractModel):
    schema_version: Literal[EVIDENCE_SCHEMA_VERSION] = EVIDENCE_SCHEMA_VERSION
    receipt_id: SafeId
    run_id: SafeId
    run_revision: int = Field(ge=0)
    ingestion_operation_key: Sha256
    canonical_request_hash: Sha256
    task_id: SafeId
    task_operation_key: Sha256
    attempt_id: SafeId
    attempt_number: int = Field(ge=1)
    agent_step: int = Field(ge=1)
    tool_call_id: SafeId
    tool_operation_key: Sha256
    adapter_id: SafeId
    observed_at: datetime
    recorded_at: datetime
    evidence_refs: tuple[EvidenceRevisionRef, ...]

    _aware_observed = field_validator("observed_at")(_require_aware)
    _aware_recorded = field_validator("recorded_at")(_require_aware)


class EvidenceCandidate(ContractModel):
    schema_version: Literal[EVIDENCE_SCHEMA_VERSION] = EVIDENCE_SCHEMA_VERSION
    run_id: SafeId
    run_revision: int = Field(ge=0)
    task_id: SafeId
    task_operation_key: Sha256
    attempt_id: SafeId
    attempt_number: int = Field(ge=1)
    agent_step: int = Field(ge=1)
    tool_call_id: SafeId
    tool_input_hash: Sha256
    tool_operation_key: Sha256
    adapter_id: SafeId
    extractor_id: SafeId
    extractor_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    source_type: SourceType
    original_locator: Locator
    canonical_locator: Locator
    evidence_scope_key: SafeId
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content: Text
    observed_at: datetime
    extraction_context: dict[str, Any] = Field(default_factory=dict)

    _safe_context = field_validator("extraction_context")(_json_object)
    _aware = field_validator("observed_at")(_require_aware)


class EvidenceIngestionItemResult(ContractModel):
    evidence_id: SafeId
    revision: int = Field(ge=1)
    disposition: EvidenceIngestionDisposition


class EvidenceIngestionResult(ContractModel):
    run_id: SafeId
    ingestion_operation_key: Sha256
    receipt_id: SafeId
    store_revision: int = Field(ge=0)
    replayed: bool = False
    items: tuple[EvidenceIngestionItemResult, ...]


class EvidenceStoreSnapshot(ContractModel):
    schema_version: Literal[EVIDENCE_SCHEMA_VERSION] = EVIDENCE_SCHEMA_VERSION
    run_id: SafeId
    store_revision: int = Field(ge=0)
    sources: tuple[SourceRecord, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    revisions: tuple[EvidenceRevision, ...] = ()
    receipts: tuple[IngestionReceipt, ...] = ()

    @model_validator(mode="after")
    def references_are_consistent(self) -> EvidenceStoreSnapshot:
        def unique(items: tuple[Any, ...], attr: str) -> bool:
            values = [getattr(item, attr) for item in items]
            return len(values) == len(set(values))

        if not unique(self.sources, "source_id") or not unique(
            self.evidence, "evidence_id"
        ):
            raise ValueError("duplicate evidence entity identity")
        if not unique(self.receipts, "receipt_id"):
            raise ValueError("duplicate ingestion receipt identity")
        source_ids = {item.source_id for item in self.sources}
        evidence_ids = {item.evidence_id for item in self.evidence}
        revision_keys = {(item.evidence_id, item.revision) for item in self.revisions}
        if len(revision_keys) != len(self.revisions):
            raise ValueError("duplicate evidence revision")
        for source in self.sources:
            expected_source_id = stable_id(
                "src",
                [self.run_id, source.source_type.value, source.canonical_locator],
            )
            if source.source_id != expected_source_id:
                raise ValueError("source identity does not match canonical locator")
        for record in self.evidence:
            if record.run_id != self.run_id or record.source_id not in source_ids:
                raise ValueError("invalid evidence source/run reference")
            entity_revisions = sorted(
                item.revision
                for item in self.revisions
                if item.evidence_id == record.evidence_id
            )
            if entity_revisions != list(range(1, record.current_revision + 1)):
                raise ValueError(
                    "evidence revisions must be continuous through current revision"
                )
            if not entity_revisions or entity_revisions[-1] != record.current_revision:
                raise ValueError("evidence current revision must be the maximum")
            expected_evidence_id = stable_id(
                "ev",
                [
                    self.run_id,
                    record.source_id,
                    record.evidence_scope_key,
                    record.media_type,
                ],
            )
            if record.evidence_id != expected_evidence_id:
                raise ValueError("evidence identity does not match source and scope")
        if any(item.evidence_id not in evidence_ids for item in self.revisions):
            raise ValueError("orphan evidence revision")
        for revision in self.revisions:
            predecessor = (
                None
                if revision.supersedes_revision is None
                else (revision.evidence_id, revision.supersedes_revision)
            )
            if predecessor is not None and predecessor not in revision_keys:
                raise ValueError("evidence revision predecessor does not exist")
            if revision.content_hash != sha256_text(revision.content):
                raise ValueError("evidence content hash differs")
            if revision.normalized_content_hash != sha256_text(
                normalize_content(revision.content)
            ):
                raise ValueError("normalized evidence content hash differs")
            if revision.extraction_context_hash != stable_hash(
                revision.extraction_context
            ):
                raise ValueError("evidence extraction context hash differs")
        for item in (*self.sources, *self.revisions, *self.receipts):
            if item.run_id != self.run_id:
                raise ValueError("snapshot contains another run")
        for receipt in self.receipts:
            if not receipt.evidence_refs:
                raise ValueError("ingestion receipt must reference evidence")
            if any(
                ref.evidence_id not in evidence_ids
                or (ref.evidence_id, ref.revision) not in revision_keys
                for ref in receipt.evidence_refs
            ):
                raise ValueError("receipt references missing evidence revision")
        return self
