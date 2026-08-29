"""Freeze Phase 5 stores and deterministically admit whole context items."""

from __future__ import annotations

from researchos.domain.claims import (
    CitationReference,
    ClaimEvidenceRelation,
    ConflictCandidate,
)
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.synthesis import FrozenVerificationInput, VerificationPolicy
from researchos.interfaces.evidence import ClaimGraphStore, EvidenceStore


def freeze_verification_input(
    *,
    run_id: str,
    run_revision: int,
    claim_store: ClaimGraphStore,
    evidence_store: EvidenceStore,
    policy: VerificationPolicy,
) -> FrozenVerificationInput:
    """Load each live store once and admit only complete claim/evidence models."""
    claims = claim_store.load(run_id)
    evidence = evidence_store.load(run_id)
    claim_hash = model_sha256(claims)
    evidence_hash = model_sha256(evidence)
    current_claims = {
        item.claim_id: item
        for item in claims.claims
        if item.lifecycle is RecordLifecycle.ACTIVE
    }
    claim_revisions = {
        (item.claim_id, item.revision): item for item in claims.claim_revisions
    }
    current_evidence = {
        item.evidence_id: item
        for item in evidence.evidence
        if item.lifecycle is RecordLifecycle.ACTIVE
    }
    evidence_revisions = {
        (item.evidence_id, item.revision): item for item in evidence.revisions
    }
    current_edges = []
    for edge in claims.edges:
        if edge.lifecycle is not RecordLifecycle.ACTIVE:
            continue
        revision = next(
            item
            for item in claims.edge_revisions
            if item.edge_id == edge.edge_id and item.revision == edge.current_revision
        )
        current_edges.append((edge, revision))

    selected_claims: list[str] = []
    omitted_claims: list[str] = []
    selected_evidence: list[str] = []
    omitted_evidence: set[str] = set()
    citations: list[CitationReference] = []
    used_bytes = 0
    for claim_id in sorted(current_claims):
        record = current_claims[claim_id]
        revision = claim_revisions[(claim_id, record.current_revision)]
        edges = sorted(
            (pair for pair in current_edges if pair[0].claim_id == claim_id),
            key=lambda pair: pair[0].evidence_id,
        )
        claim_size = len(canonical_json_bytes(record)) + len(
            canonical_json_bytes(revision)
        )
        if (
            len(selected_claims) >= policy.max_claims
            or used_bytes + claim_size > policy.max_model_context_bytes
        ):
            omitted_claims.append(claim_id)
            omitted_evidence.update(
                edge.evidence_id
                for edge, _edge_revision in edges
                if edge.evidence_id in current_evidence
            )
            continue
        selected_claims.append(claim_id)
        used_bytes += claim_size
        admitted_for_claim = 0
        for edge, _edge_revision in edges:
            item = current_evidence.get(edge.evidence_id)
            item_revision = (
                None
                if item is None
                else evidence_revisions.get((item.evidence_id, item.current_revision))
            )
            if item is None or item_revision is None:
                continue
            item_size = len(canonical_json_bytes(item)) + len(
                canonical_json_bytes(item_revision)
            )
            if (
                admitted_for_claim >= policy.max_evidence_per_claim
                or used_bytes + item_size > policy.max_model_context_bytes
            ):
                omitted_evidence.add(item.evidence_id)
                continue
            admitted_for_claim += 1
            used_bytes += item_size
            if item.evidence_id not in selected_evidence:
                selected_evidence.append(item.evidence_id)
            source = next(
                source
                for source in evidence.sources
                if source.source_id == item.source_id
            )
            citations.append(
                CitationReference(
                    citation_id=stable_id(
                        "cit",
                        [
                            run_id,
                            claim_id,
                            revision.revision,
                            item.evidence_id,
                            item_revision.revision,
                            source.source_id,
                        ],
                    ),
                    claim_id=claim_id,
                    claim_revision=revision.revision,
                    evidence_id=item.evidence_id,
                    evidence_revision=item_revision.revision,
                    source_id=source.source_id,
                    expected_evidence_content_hash=item_revision.content_hash,
                )
            )

    selected_set = set(selected_evidence)
    omitted_evidence.difference_update(selected_set)
    conflicts: list[ConflictCandidate] = []
    for claim_id in selected_claims:
        supporting: list[str] = []
        contradicting: list[str] = []
        for edge, revision in current_edges:
            if edge.claim_id != claim_id or edge.evidence_id not in selected_set:
                continue
            if revision.relation is ClaimEvidenceRelation.SUPPORTS:
                supporting.append(edge.evidence_id)
            elif revision.relation is ClaimEvidenceRelation.CONTRADICTS:
                contradicting.append(edge.evidence_id)
        if supporting and contradicting:
            conflicts.append(
                ConflictCandidate(
                    claim_id=claim_id,
                    supporting_evidence_ids=tuple(sorted(supporting)),
                    contradicting_evidence_ids=tuple(sorted(contradicting)),
                )
            )
    base = {
        "run_id": run_id,
        "run_revision": run_revision,
        "claim_snapshot_hash": claim_hash,
        "evidence_snapshot_hash": evidence_hash,
        "selected_claim_ids": selected_claims,
        "omitted_claim_ids": omitted_claims,
        "selected_evidence_ids": selected_evidence,
        "omitted_evidence_ids": sorted(omitted_evidence),
        "allowed_citation_ids": sorted(item.citation_id for item in citations),
    }
    return FrozenVerificationInput(
        run_id=run_id,
        run_revision=run_revision,
        claim_snapshot=claims,
        evidence_snapshot=evidence,
        claim_snapshot_hash=claim_hash,
        evidence_snapshot_hash=evidence_hash,
        selected_claim_ids=tuple(selected_claims),
        omitted_claim_ids=tuple(omitted_claims),
        selected_evidence_ids=tuple(sorted(selected_evidence)),
        omitted_evidence_ids=tuple(sorted(omitted_evidence)),
        allowed_citations=tuple(sorted(citations, key=lambda item: item.citation_id)),
        conflict_candidates=tuple(sorted(conflicts, key=lambda item: item.claim_id)),
        frozen_input_hash=stable_hash(base),
    )


def compute_verification_id(
    frozen: FrozenVerificationInput,
    policy: VerificationPolicy,
    model_bundle_hash: str,
) -> str:
    return stable_id(
        "ver",
        [
            frozen.run_id,
            frozen.run_revision,
            frozen.claim_snapshot_hash,
            frozen.evidence_snapshot_hash,
            model_sha256(policy),
            model_bundle_hash,
        ],
    )
