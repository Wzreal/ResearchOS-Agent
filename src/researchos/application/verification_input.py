"""Freeze Phase 5 stores and deterministically admit whole context items."""

from __future__ import annotations

import json

from researchos.application.errors import (
    VerificationInputCorruption,
    VerificationPreconditionError,
)
from researchos.domain.claims import (
    CitationReference,
    ClaimEvidenceRelation,
    ConflictCandidate,
    EdgeView,
)
from researchos.domain.contracts import model_sha256
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.synthesis import FrozenVerificationInput, VerificationPolicy
from researchos.interfaces.evidence import ClaimGraphStore, EvidenceStore


def _context_bytes(
    *,
    claims: list[dict[str, object]],
    evidence: list[dict[str, object]],
    citations: list[CitationReference],
    conflicts: list[ConflictCandidate],
    omitted_claim_ids: list[str],
    omitted_evidence_ids: list[str],
) -> int:
    payload = {
        "frozen_input_hash": "0" * 64,
        "claims": claims,
        "evidence": evidence,
        "citation_allowlist": [item.model_dump(mode="json") for item in citations],
        "conflicts": [item.model_dump(mode="json") for item in conflicts],
        "omitted_claim_ids": omitted_claim_ids,
        "omitted_evidence_ids": omitted_evidence_ids,
    }
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def derive_current_valid_edges(
    claim_snapshot, evidence_snapshot
) -> tuple[EdgeView, ...]:
    """Return the one fail-closed definition of a Phase 6 current-valid edge."""
    claims = {item.claim_id: item for item in claim_snapshot.claims}
    claim_revision_keys = {
        (item.claim_id, item.revision) for item in claim_snapshot.claim_revisions
    }
    evidence = {item.evidence_id: item for item in evidence_snapshot.evidence}
    evidence_revision_keys = {
        (item.evidence_id, item.revision) for item in evidence_snapshot.revisions
    }
    edge_revisions = {
        (item.edge_id, item.revision): item for item in claim_snapshot.edge_revisions
    }
    valid: list[EdgeView] = []
    for edge in claim_snapshot.edges:
        if edge.lifecycle is not RecordLifecycle.ACTIVE:
            continue
        claim = claims.get(edge.claim_id)
        evidence_record = evidence.get(edge.evidence_id)
        revision = edge_revisions.get((edge.edge_id, edge.current_revision))
        if claim is None or evidence_record is None or revision is None:
            raise VerificationInputCorruption("active edge has a dangling entity pin")
        if (edge.claim_id, revision.claim_revision) not in claim_revision_keys:
            raise VerificationInputCorruption("active edge claim revision is absent")
        if (edge.evidence_id, revision.evidence_revision) not in evidence_revision_keys:
            raise VerificationInputCorruption("active edge evidence revision is absent")
        if (
            claim.lifecycle is RecordLifecycle.ACTIVE
            and evidence_record.lifecycle is RecordLifecycle.ACTIVE
            and revision.claim_revision == claim.current_revision
            and revision.evidence_revision == evidence_record.current_revision
        ):
            valid.append(EdgeView(edge=edge, revision=revision))
    return tuple(sorted(valid, key=lambda item: item.edge.edge_id))


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
    current_edges = derive_current_valid_edges(claims, evidence)

    selected_claims: list[str] = []
    omitted_claims: list[str] = []
    selected_evidence: list[str] = []
    omitted_evidence: set[str] = set()
    citations: list[CitationReference] = []
    claim_payloads: list[dict[str, object]] = []
    evidence_payloads: list[dict[str, object]] = []
    for claim_id in sorted(current_claims):
        record = current_claims[claim_id]
        revision = claim_revisions[(claim_id, record.current_revision)]
        edges = sorted(
            (view for view in current_edges if view.edge.claim_id == claim_id),
            key=lambda view: (
                {
                    ClaimEvidenceRelation.CONTRADICTS: 0,
                    ClaimEvidenceRelation.SUPPORTS: 1,
                    ClaimEvidenceRelation.CONTEXTUALIZES: 2,
                }[view.revision.relation],
                view.edge.edge_id,
                view.edge.evidence_id,
            ),
        )
        claim_payload = {
            "record": record.model_dump(mode="json"),
            "revision": revision.model_dump(mode="json"),
        }
        trial_size = _context_bytes(
            claims=[*claim_payloads, claim_payload],
            evidence=evidence_payloads,
            citations=citations,
            conflicts=[],
            omitted_claim_ids=omitted_claims,
            omitted_evidence_ids=sorted(omitted_evidence),
        )
        if (
            len(selected_claims) >= policy.max_claims
            or trial_size > policy.max_model_context_bytes
        ):
            omitted_claims.append(claim_id)
            omitted_evidence.update(
                view.edge.evidence_id
                for view in edges
                if view.edge.evidence_id in current_evidence
            )
            continue
        selected_claims.append(claim_id)
        claim_payloads.append(claim_payload)
        admitted_for_claim = 0
        for view in edges:
            edge = view.edge
            item = current_evidence.get(edge.evidence_id)
            item_revision = (
                None
                if item is None
                else evidence_revisions.get((item.evidence_id, item.current_revision))
            )
            if item is None or item_revision is None:
                continue
            item_payload = {
                "record": item.model_dump(mode="json"),
                "revision": item_revision.model_dump(mode="json"),
            }
            source = next(
                source
                for source in evidence.sources
                if source.source_id == item.source_id
            )
            citation = CitationReference(
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
            new_evidence_payloads = (
                evidence_payloads
                if item.evidence_id in selected_evidence
                else [*evidence_payloads, item_payload]
            )
            trial_size = _context_bytes(
                claims=claim_payloads,
                evidence=new_evidence_payloads,
                citations=[*citations, citation],
                conflicts=[],
                omitted_claim_ids=omitted_claims,
                omitted_evidence_ids=sorted(omitted_evidence),
            )
            if (
                admitted_for_claim >= policy.max_evidence_per_claim
                or trial_size > policy.max_model_context_bytes
            ):
                omitted_evidence.add(item.evidence_id)
                continue
            admitted_for_claim += 1
            if item.evidence_id not in selected_evidence:
                selected_evidence.append(item.evidence_id)
                evidence_payloads.append(item_payload)
            citations.append(citation)

    selected_set = set(selected_evidence)
    omitted_evidence.difference_update(selected_set)
    conflicts: list[ConflictCandidate] = []
    for claim_id in selected_claims:
        supporting: list[str] = []
        contradicting: list[str] = []
        for view in current_edges:
            edge, revision = view.edge, view.revision
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
    final_context_bytes = _context_bytes(
        claims=claim_payloads,
        evidence=evidence_payloads,
        citations=citations,
        conflicts=conflicts,
        omitted_claim_ids=omitted_claims,
        omitted_evidence_ids=sorted(omitted_evidence),
    )
    if final_context_bytes > policy.max_model_context_bytes:
        raise VerificationPreconditionError(
            "frozen verification context metadata exceeds byte limit"
        )
    return FrozenVerificationInput(
        run_id=run_id,
        run_revision=run_revision,
        claim_snapshot=claims,
        evidence_snapshot=evidence,
        claim_snapshot_hash=claim_hash,
        evidence_snapshot_hash=evidence_hash,
        selected_claim_ids=tuple(selected_claims),
        omitted_claim_ids=tuple(omitted_claims),
        selected_evidence_ids=tuple(selected_evidence),
        omitted_evidence_ids=tuple(sorted(omitted_evidence)),
        allowed_citations=tuple(sorted(citations, key=lambda item: item.citation_id)),
        current_valid_edges=current_edges,
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
