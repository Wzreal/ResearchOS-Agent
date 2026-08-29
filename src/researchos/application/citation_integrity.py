"""Structural-only citation integrity validation for Phase 5."""

from __future__ import annotations

from researchos.domain.claims import (
    CitationIntegrityIssue,
    CitationIntegrityResult,
    CitationIssueCode,
    CitationReference,
    CitationSeverity,
)
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.identity import sha256_text, stable_id
from researchos.interfaces.evidence import ClaimGraphStore, EvidenceStore


class CitationIntegrityValidator:
    def __init__(
        self, *, claim_store: ClaimGraphStore, evidence_store: EvidenceStore
    ) -> None:
        self._claims = claim_store
        self._evidence = evidence_store

    def validate(
        self, run_id: str, references: tuple[CitationReference, ...] = ()
    ) -> CitationIntegrityResult:
        claims = self._claims.load(run_id)
        evidence = self._evidence.load(run_id)
        issues: list[CitationIntegrityIssue] = []
        claim_by_id = {item.claim_id: item for item in claims.claims}
        evidence_by_id = {item.evidence_id: item for item in evidence.evidence}
        source_by_id = {item.source_id: item for item in evidence.sources}
        claim_revisions = {
            (item.claim_id, item.revision): item for item in claims.claim_revisions
        }
        evidence_revisions = {
            (item.evidence_id, item.revision): item for item in evidence.revisions
        }
        references_by_id: dict[str, CitationReference] = {}
        for reference in references:
            prior = references_by_id.get(reference.citation_id)
            if prior is not None and prior != reference:
                issues.append(
                    self._issue(
                        CitationIssueCode.CONFLICTING_DUPLICATE_CITATION_ID,
                        CitationSeverity.ERROR,
                        "citation ID is reused for another reference",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
            else:
                references_by_id[reference.citation_id] = reference
        for source in evidence.sources:
            expected_source = stable_id(
                "src",
                [run_id, source.source_type.value, source.canonical_locator],
            )
            if expected_source != source.source_id:
                issues.append(
                    self._issue(
                        CitationIssueCode.IDENTITY_MISMATCH,
                        CitationSeverity.ERROR,
                        "source identity does not match its canonical locator",
                    )
                )
        for record in evidence.evidence:
            source = source_by_id.get(record.source_id)
            if source is None:
                issues.append(
                    self._issue(
                        CitationIssueCode.DANGLING_SOURCE,
                        CitationSeverity.ERROR,
                        "evidence references a missing source",
                        evidence_id=record.evidence_id,
                    )
                )
                continue
            expected = stable_id(
                "ev",
                [
                    run_id,
                    source.source_id,
                    record.evidence_scope_key,
                    record.media_type,
                ],
            )
            if expected != record.evidence_id:
                issues.append(
                    self._issue(
                        CitationIssueCode.IDENTITY_MISMATCH,
                        CitationSeverity.ERROR,
                        "evidence identity does not match source and scope",
                        evidence_id=record.evidence_id,
                    )
                )
        for revision in evidence.revisions:
            if sha256_text(revision.content) != revision.content_hash:
                issues.append(
                    self._issue(
                        CitationIssueCode.CONTENT_HASH_MISMATCH,
                        CitationSeverity.ERROR,
                        "evidence content hash differs",
                        evidence_id=revision.evidence_id,
                    )
                )
        for edge in claims.edges:
            if edge.claim_id not in claim_by_id:
                issues.append(
                    self._issue(
                        CitationIssueCode.DANGLING_CLAIM,
                        CitationSeverity.ERROR,
                        "relation references a missing claim",
                        claim_id=edge.claim_id,
                        evidence_id=edge.evidence_id,
                    )
                )
            if edge.evidence_id not in evidence_by_id:
                issues.append(
                    self._issue(
                        CitationIssueCode.DANGLING_EVIDENCE,
                        CitationSeverity.ERROR,
                        "relation references missing evidence",
                        claim_id=edge.claim_id,
                        evidence_id=edge.evidence_id,
                    )
                )
        for revision in claims.edge_revisions:
            edge = next(
                (item for item in claims.edges if item.edge_id == revision.edge_id),
                None,
            )
            if edge is None:
                issues.append(
                    self._issue(
                        CitationIssueCode.IDENTITY_MISMATCH,
                        CitationSeverity.ERROR,
                        "edge revision has no edge identity",
                    )
                )
                continue
            if (edge.claim_id, revision.claim_revision) not in claim_revisions:
                issues.append(
                    self._issue(
                        CitationIssueCode.MISSING_REVISION,
                        CitationSeverity.ERROR,
                        "relation claim revision does not exist",
                        claim_id=edge.claim_id,
                        evidence_id=edge.evidence_id,
                    )
                )
            if (edge.evidence_id, revision.evidence_revision) not in evidence_revisions:
                issues.append(
                    self._issue(
                        CitationIssueCode.MISSING_REVISION,
                        CitationSeverity.ERROR,
                        "relation evidence revision does not exist",
                        claim_id=edge.claim_id,
                        evidence_id=edge.evidence_id,
                    )
                )
        for reference in references:
            claim = claim_by_id.get(reference.claim_id)
            item = evidence_by_id.get(reference.evidence_id)
            cited_source = source_by_id.get(reference.source_id)
            if cited_source is None:
                issues.append(
                    self._issue(
                        CitationIssueCode.DANGLING_SOURCE,
                        CitationSeverity.ERROR,
                        "citation source does not exist",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
            if claim is None:
                issues.append(
                    self._issue(
                        CitationIssueCode.DANGLING_CLAIM,
                        CitationSeverity.ERROR,
                        "citation claim does not exist",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
            elif (reference.claim_id, reference.claim_revision) not in claim_revisions:
                issues.append(
                    self._issue(
                        CitationIssueCode.MISSING_REVISION,
                        CitationSeverity.ERROR,
                        "citation claim revision does not exist",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
            elif reference.claim_revision != claim.current_revision:
                issues.append(
                    self._issue(
                        CitationIssueCode.STALE_CLAIM_REVISION,
                        CitationSeverity.WARNING,
                        "citation uses a valid superseded claim revision",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
            if claim is not None and claim.lifecycle is RecordLifecycle.TOMBSTONED:
                issues.append(
                    self._issue(
                        CitationIssueCode.TOMBSTONED_CLAIM,
                        CitationSeverity.WARNING,
                        "citation claim is tombstoned but remains addressable",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
            if item is None:
                issues.append(
                    self._issue(
                        CitationIssueCode.DANGLING_EVIDENCE,
                        CitationSeverity.ERROR,
                        "citation evidence does not exist",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
            else:
                if item.source_id != reference.source_id:
                    issues.append(
                        self._issue(
                            CitationIssueCode.CITATION_SOURCE_MISMATCH,
                            CitationSeverity.ERROR,
                            "citation source does not match its evidence",
                            claim_id=reference.claim_id,
                            evidence_id=reference.evidence_id,
                            citation_id=reference.citation_id,
                        )
                    )
            cited_revision = evidence_revisions.get(
                (reference.evidence_id, reference.evidence_revision)
            )
            if cited_revision is None:
                issues.append(
                    self._issue(
                        CitationIssueCode.MISSING_REVISION,
                        CitationSeverity.ERROR,
                        "citation evidence revision does not exist",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
            else:
                if (
                    cited_revision.content_hash
                    != reference.expected_evidence_content_hash
                ):
                    issues.append(
                        self._issue(
                            CitationIssueCode.CITATION_CONTENT_HASH_MISMATCH,
                            CitationSeverity.ERROR,
                            "citation content pin does not match its revision",
                            claim_id=reference.claim_id,
                            evidence_id=reference.evidence_id,
                            citation_id=reference.citation_id,
                        )
                    )
                if item is not None and (
                    reference.evidence_revision != item.current_revision
                ):
                    issues.append(
                        self._issue(
                            CitationIssueCode.STALE_EVIDENCE_REVISION,
                            CitationSeverity.WARNING,
                            "citation uses a valid superseded evidence revision",
                            claim_id=reference.claim_id,
                            evidence_id=reference.evidence_id,
                            citation_id=reference.citation_id,
                        )
                    )
            if item is not None and item.lifecycle is RecordLifecycle.TOMBSTONED:
                issues.append(
                    self._issue(
                        CitationIssueCode.TOMBSTONED_EVIDENCE,
                        CitationSeverity.WARNING,
                        "citation evidence is tombstoned but remains addressable",
                        claim_id=reference.claim_id,
                        evidence_id=reference.evidence_id,
                        citation_id=reference.citation_id,
                    )
                )
        ordered = tuple(
            sorted(
                issues,
                key=lambda x: (
                    x.severity.value,
                    x.code.value,
                    x.claim_id or "",
                    x.evidence_id or "",
                ),
            )
        )
        return CitationIntegrityResult(
            valid=not any(item.severity is CitationSeverity.ERROR for item in ordered),
            issues=ordered,
        )

    @staticmethod
    def _issue(
        code: CitationIssueCode,
        severity: CitationSeverity,
        message: str,
        *,
        claim_id: str | None = None,
        evidence_id: str | None = None,
        citation_id: str | None = None,
    ) -> CitationIntegrityIssue:
        return CitationIntegrityIssue(
            code=code,
            severity=severity,
            message=message,
            claim_id=claim_id,
            evidence_id=evidence_id,
            citation_id=citation_id,
        )
