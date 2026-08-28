"""Phase 5 claim graph and citation-integrity contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from researchos.domain.contracts import ContractModel, SafeId, Sha256, _require_aware
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.identity import normalize_content, sha256_text, stable_id

CLAIM_SCHEMA_VERSION = 1
Statement = Annotated[str, StringConstraints(min_length=1, max_length=20_000)]


class ClaimEvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXTUALIZES = "contextualizes"


class CitationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class CitationIssueCode(StrEnum):
    DANGLING_CLAIM = "dangling_claim"
    DANGLING_EVIDENCE = "dangling_evidence"
    DANGLING_SOURCE = "dangling_source"
    CONTENT_HASH_MISMATCH = "content_hash_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    MISSING_REVISION = "missing_revision"
    STALE_CLAIM_REVISION = "stale_claim_revision"
    STALE_EVIDENCE_REVISION = "stale_evidence_revision"
    TOMBSTONED_CLAIM = "tombstoned_claim"
    TOMBSTONED_EVIDENCE = "tombstoned_evidence"
    CONFLICTING_DUPLICATE_CITATION_ID = "conflicting_duplicate_citation_id"


class ClaimRecord(ContractModel):
    schema_version: Literal[CLAIM_SCHEMA_VERSION] = CLAIM_SCHEMA_VERSION
    run_id: SafeId
    claim_id: SafeId
    claim_scope_key: SafeId
    current_revision: int = Field(ge=1)
    lifecycle: RecordLifecycle = RecordLifecycle.ACTIVE
    created_at: datetime
    updated_at: datetime

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)


class ClaimGenerationContext(ContractModel):
    """Safe provenance for one statement revision, never claim identity."""

    run_revision: int = Field(ge=0)
    producer_id: SafeId
    task_id: SafeId | None = None
    source_operation_key: Sha256 | None = None


class ClaimRevision(ContractModel):
    schema_version: Literal[CLAIM_SCHEMA_VERSION] = CLAIM_SCHEMA_VERSION
    run_id: SafeId
    claim_id: SafeId
    revision: int = Field(ge=1)
    statement: Statement
    normalized_statement_hash: Sha256
    generation_context: ClaimGenerationContext | None = None
    created_at: datetime
    supersedes_revision: int | None = Field(default=None, ge=1)

    _aware = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def chain_is_local(self) -> ClaimRevision:
        expected = None if self.revision == 1 else self.revision - 1
        if self.supersedes_revision != expected:
            raise ValueError("claim revision must supersede its immediate predecessor")
        return self


class ClaimMutationReceipt(ContractModel):
    receipt_id: SafeId
    run_id: SafeId
    operation_key: Sha256
    request_hash: Sha256
    claim_id: SafeId
    claim_revision: int = Field(ge=1)
    edge_id: SafeId | None = None
    edge_revision: int | None = Field(default=None, ge=1)
    recorded_at: datetime

    _aware = field_validator("recorded_at")(_require_aware)

    @model_validator(mode="after")
    def edge_reference_is_complete(self) -> ClaimMutationReceipt:
        if (self.edge_id is None) != (self.edge_revision is None):
            raise ValueError("edge mutation reference must be complete")
        return self


class ClaimEvidenceEdge(ContractModel):
    schema_version: Literal[CLAIM_SCHEMA_VERSION] = CLAIM_SCHEMA_VERSION
    run_id: SafeId
    edge_id: SafeId
    claim_id: SafeId
    evidence_id: SafeId
    current_revision: int = Field(ge=1)
    lifecycle: RecordLifecycle = RecordLifecycle.ACTIVE
    created_at: datetime
    updated_at: datetime

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)


class ClaimEvidenceEdgeRevision(ContractModel):
    schema_version: Literal[CLAIM_SCHEMA_VERSION] = CLAIM_SCHEMA_VERSION
    run_id: SafeId
    edge_id: SafeId
    revision: int = Field(ge=1)
    relation: ClaimEvidenceRelation
    claim_revision: int = Field(ge=1)
    evidence_revision: int = Field(ge=1)
    created_at: datetime
    supersedes_revision: int | None = Field(default=None, ge=1)

    _aware = field_validator("created_at")(_require_aware)

    @model_validator(mode="after")
    def chain_is_local(self) -> ClaimEvidenceEdgeRevision:
        expected = None if self.revision == 1 else self.revision - 1
        if self.supersedes_revision != expected:
            raise ValueError("edge revision must supersede its immediate predecessor")
        return self


class ClaimGraphSnapshot(ContractModel):
    schema_version: Literal[CLAIM_SCHEMA_VERSION] = CLAIM_SCHEMA_VERSION
    run_id: SafeId
    store_revision: int = Field(ge=0)
    claims: tuple[ClaimRecord, ...] = ()
    claim_revisions: tuple[ClaimRevision, ...] = ()
    edges: tuple[ClaimEvidenceEdge, ...] = ()
    edge_revisions: tuple[ClaimEvidenceEdgeRevision, ...] = ()
    receipts: tuple[ClaimMutationReceipt, ...] = ()

    @model_validator(mode="after")
    def graph_is_consistent(self) -> ClaimGraphSnapshot:
        claim_ids = [item.claim_id for item in self.claims]
        edge_ids = [item.edge_id for item in self.edges]
        claim_id_set = set(claim_ids)
        edge_id_set = set(edge_ids)
        pairs = [(item.claim_id, item.evidence_id) for item in self.edges]
        if len(claim_ids) != len(set(claim_ids)) or len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate graph entity identity")
        if len(pairs) != len(set(pairs)):
            raise ValueError(
                "only one current relation is allowed per claim/evidence pair"
            )
        claim_revision_keys = {
            (item.claim_id, item.revision) for item in self.claim_revisions
        }
        edge_revision_keys = {
            (item.edge_id, item.revision) for item in self.edge_revisions
        }
        if len(claim_revision_keys) != len(self.claim_revisions) or len(
            edge_revision_keys
        ) != len(self.edge_revisions):
            raise ValueError("duplicate graph revision")
        for item in (
            *self.claims,
            *self.claim_revisions,
            *self.edges,
            *self.edge_revisions,
            *self.receipts,
        ):
            if item.run_id != self.run_id:
                raise ValueError("graph contains another run")
        for claim in self.claims:
            if (claim.claim_id, claim.current_revision) not in claim_revision_keys:
                raise ValueError("claim current revision does not exist")
            if claim.claim_id != stable_id(
                "claim", [self.run_id, claim.claim_scope_key]
            ):
                raise ValueError("claim identity does not match immutable scope")
        if any(item.claim_id not in claim_id_set for item in self.claim_revisions):
            raise ValueError("orphan claim revision")
        for revision in self.claim_revisions:
            if revision.normalized_statement_hash != sha256_text(
                normalize_content(revision.statement)
            ):
                raise ValueError("normalized claim statement hash differs")
        for edge in self.edges:
            if (
                edge.claim_id not in claim_id_set
                or (edge.edge_id, edge.current_revision) not in edge_revision_keys
            ):
                raise ValueError("edge has a dangling claim/current revision")
            if edge.edge_id != stable_id(
                "edge", [self.run_id, edge.claim_id, edge.evidence_id]
            ):
                raise ValueError("edge identity does not match claim/evidence pair")
        if any(item.edge_id not in edge_id_set for item in self.edge_revisions):
            raise ValueError("orphan edge revision")
        for receipt in self.receipts:
            if (
                receipt.claim_id not in claim_id_set
                or (receipt.claim_id, receipt.claim_revision) not in claim_revision_keys
            ):
                raise ValueError("claim receipt references missing claim revision")
            if receipt.edge_id is not None and (
                receipt.edge_id not in edge_id_set
                or (receipt.edge_id, receipt.edge_revision) not in edge_revision_keys
            ):
                raise ValueError("claim receipt references missing edge revision")
        return self


class ClaimView(ContractModel):
    claim: ClaimRecord
    revision: ClaimRevision


class EdgeView(ContractModel):
    edge: ClaimEvidenceEdge
    revision: ClaimEvidenceEdgeRevision


class ConflictCandidate(ContractModel):
    claim_id: SafeId
    supporting_evidence_ids: tuple[SafeId, ...]
    contradicting_evidence_ids: tuple[SafeId, ...]


class CitationReference(ContractModel):
    citation_id: SafeId
    claim_id: SafeId
    claim_revision: int = Field(ge=1)
    evidence_id: SafeId
    evidence_revision: int = Field(ge=1)


class CitationIntegrityIssue(ContractModel):
    code: CitationIssueCode
    severity: CitationSeverity
    claim_id: SafeId | None = None
    evidence_id: SafeId | None = None
    citation_id: SafeId | None = None
    message: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class CitationIntegrityResult(ContractModel):
    valid: bool
    issues: tuple[CitationIntegrityIssue, ...]

    @computed_field
    @property
    def has_warnings(self) -> bool:
        return any(item.severity is CitationSeverity.WARNING for item in self.issues)

    @model_validator(mode="after")
    def validity_matches_errors(self) -> CitationIntegrityResult:
        expected = not any(
            item.severity is CitationSeverity.ERROR for item in self.issues
        )
        if self.valid != expected:
            raise ValueError("valid must mean that no ERROR issue exists")
        return self
