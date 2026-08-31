"""Independent Phase 8 verification lineage reconstruction."""

from __future__ import annotations

from researchos.application.errors import VerificationRecoveryInconsistency
from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.identity import stable_hash
from researchos.domain.synthesis import FrozenVerificationInput, VerificationResult
from researchos.domain.verification_runtime import VerificationLineage


def reconstruct_verification_lineage(
    result: VerificationResult, frozen: FrozenVerificationInput
) -> VerificationLineage:
    if (
        result.verification_id == ""
        or result.run_id != frozen.run_id
        or result.frozen_input_hash != frozen.frozen_input_hash
    ):
        raise VerificationRecoveryInconsistency(
            "verification authority and frozen lineage input differ"
        )
    current_edges = {
        (item.edge.claim_id, item.edge.evidence_id): item
        for item in frozen.current_valid_edges
    }
    allowed_citation_pins = {
        (
            item.claim_id,
            item.claim_revision,
            item.evidence_id,
            item.evidence_revision,
            item.source_id,
            item.expected_evidence_content_hash,
        )
        for item in frozen.allowed_citations
    }
    evidence_records = {
        item.evidence_id: item for item in frozen.evidence_snapshot.evidence
    }
    evidence_revisions_by_key = {
        (item.evidence_id, item.revision): item
        for item in frozen.evidence_snapshot.revisions
    }
    receipt_keys = {
        (reference.evidence_id, reference.revision)
        for receipt in frozen.evidence_snapshot.receipts
        for reference in receipt.evidence_refs
    }
    edge_ids: set[str] = set()
    evidence_revisions: set[tuple[str, int]] = set()
    expected_assignments: set[tuple[str, str]] = set()
    for citation in result.final_draft.citations:
        citation_pin = (
            citation.claim_id,
            citation.claim_revision,
            citation.evidence_id,
            citation.evidence_revision,
            citation.source_id,
            citation.expected_evidence_content_hash,
        )
        if citation_pin not in allowed_citation_pins:
            raise VerificationRecoveryInconsistency(
                "final citation differs from frozen citation pins"
            )
        view = current_edges.get((citation.claim_id, citation.evidence_id))
        if view is None:
            raise VerificationRecoveryInconsistency(
                "final citation lacks a current-valid edge"
            )
        if (
            view.revision.relation is not ClaimEvidenceRelation.SUPPORTS
            or
            view.revision.claim_revision != citation.claim_revision
            or view.revision.evidence_revision != citation.evidence_revision
        ):
            raise VerificationRecoveryInconsistency(
                "final citation lacks a current SUPPORTS edge revision"
            )
        evidence_record = evidence_records.get(citation.evidence_id)
        evidence_revision = evidence_revisions_by_key.get(
            (citation.evidence_id, citation.evidence_revision)
        )
        if (
            evidence_record is None
            or evidence_revision is None
            or evidence_record.source_id != citation.source_id
            or evidence_revision.content_hash
            != citation.expected_evidence_content_hash
        ):
            raise VerificationRecoveryInconsistency(
                "final citation evidence content/source pin differs"
            )
        if (
            citation.evidence_id,
            citation.evidence_revision,
        ) not in receipt_keys:
            raise VerificationRecoveryInconsistency(
                "final citation lacks evidence ingestion provenance"
            )
        edge_ids.add(view.edge.edge_id)
        evidence_revisions.add((citation.evidence_id, citation.evidence_revision))
        expected_assignments.add((citation.citation_id, view.edge.edge_id))
    reported_assignments = {
        (item.citation.citation_id, item.edge_id)
        for item in result.citation_assignments
    }
    receipts = []
    for receipt in frozen.evidence_snapshot.receipts:
        if any(
            (reference.evidence_id, reference.revision) in evidence_revisions
            for reference in receipt.evidence_refs
        ):
            receipts.append(receipt)
    payload = {
        "verification_id": result.verification_id,
        "citation_ids": sorted(
            item.citation_id for item in result.final_draft.citations
        ),
        "current_edge_ids": sorted(edge_ids),
        "evidence_receipt_ids": sorted(item.receipt_id for item in receipts),
        "task_ids": sorted({item.task_id for item in receipts}),
        "task_operation_keys": sorted(
            {item.task_operation_key for item in receipts}
        ),
        "attempt_ids": sorted({item.attempt_id for item in receipts}),
        "tool_call_ids": sorted({item.tool_call_id for item in receipts}),
        "tool_operation_keys": sorted(
            {item.tool_operation_key for item in receipts}
        ),
        "adapter_ids": sorted({item.adapter_id for item in receipts}),
        "diagnostic_assignment_match": reported_assignments == expected_assignments,
    }
    return VerificationLineage(
        **payload,
        lineage_hash=stable_hash(payload),
    )
