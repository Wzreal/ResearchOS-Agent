"""Deterministic Phase 6 Markdown rendering."""

from __future__ import annotations

from researchos.domain.claims import CitationIntegrityIssue, CitationSeverity
from researchos.domain.synthesis import (
    AcquisitionRequest,
    JudgeDecision,
    JudgeVerdict,
    PublicationState,
    RedFinding,
    ReportDraftRevision,
    StructuralSupport,
    VerificationDisposition,
)


def _plain_text(value: str) -> str:
    """Escape model text so only the runtime can create Markdown structure."""
    escaped = value.replace("\\", "\\\\")
    for marker in (
        "`",
        "*",
        "_",
        "^",
        "{",
        "}",
        "[",
        "]",
        "(",
        ")",
        "#",
        "+",
        "-",
        ".",
        "!",
        "|",
        ">",
        "<",
    ):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped.replace("\n", " ")


def is_publishable(
    state: PublicationState,
    verdict: JudgeVerdict,
    *,
    has_open_blocking_finding: bool = False,
    has_citation_error: bool = False,
) -> bool:
    return (
        state is PublicationState.INCLUDED
        and verdict in {JudgeVerdict.SUPPORTED, JudgeVerdict.QUALIFIED}
        and not has_open_blocking_finding
        and not has_citation_error
    )


def render_markdown(
    draft: ReportDraftRevision,
    decisions: tuple[JudgeDecision, ...],
    *,
    findings: tuple[RedFinding, ...] = (),
    resolved_finding_ids: tuple[str, ...] = (),
    citation_issues: tuple[CitationIntegrityIssue, ...] = (),
    disposition: VerificationDisposition = VerificationDisposition.INCONCLUSIVE,
    acquisition_requests: tuple[AcquisitionRequest, ...] = (),
    omitted_claim_ids: tuple[str, ...] = (),
) -> bytes:
    verdicts = {item.report_claim_id: item.verdict for item in decisions}
    resolved = set(resolved_finding_ids)
    blocked = {
        item.report_claim_id for item in findings if item.finding_id not in resolved
    }
    citation_error_claims = {
        item.claim_id
        for item in citation_issues
        if item.severity is CitationSeverity.ERROR and item.claim_id is not None
    }
    global_citation_error = any(
        item.severity is CitationSeverity.ERROR and item.claim_id is None
        for item in citation_issues
    )
    citations_by_claim: dict[str, list[str]] = {}
    for citation in sorted(draft.citations, key=lambda item: item.citation_id):
        report_claim = next(
            item for item in draft.report_claims if item.claim_id == citation.claim_id
        )
        citations_by_claim.setdefault(report_claim.report_claim_id, []).append(
            citation.citation_id
        )
    lines = [
        "# Research Report",
        "",
        f"Verification disposition: `{disposition.value}`",
        "",
    ]
    published_report_claim_ids: set[str] = set()
    for section in sorted(
        draft.sections, key=lambda item: (item.ordinal, item.section_id)
    ):
        included = [
            item
            for item in draft.report_claims
            if item.section_id == section.section_id
            and is_publishable(
                item.publication_state,
                verdicts.get(item.report_claim_id, JudgeVerdict.UNRESOLVED),
                has_open_blocking_finding=item.report_claim_id in blocked,
                has_citation_error=(
                    global_citation_error or item.claim_id in citation_error_claims
                ),
            )
        ]
        if not included:
            continue
        lines.extend([f"## {_plain_text(section.title)}", ""])
        for claim in sorted(included, key=lambda item: item.report_claim_id):
            published_report_claim_ids.add(claim.report_claim_id)
            pins = "".join(
                f" [^{citation_id}]"
                for citation_id in citations_by_claim.get(claim.report_claim_id, [])
            )
            lines.extend([f"{_plain_text(claim.prose)}{pins}", ""])
    conflicts = sorted(
        item.report_claim_id
        for item in draft.report_claims
        if item.structural_support is StructuralSupport.CONFLICTED
    )
    if conflicts:
        lines.extend(["## Disclosed conflicts", ""])
        lines.extend(f"- `{item}`" for item in conflicts)
        lines.append("")
    unresolved = sorted(
        item.report_claim_id
        for item in draft.report_claims
        if item.report_claim_id not in published_report_claim_ids
    )
    if omitted_claim_ids or unresolved:
        lines.extend(["## Omitted or unresolved claims", ""])
        lines.extend(f"- omitted Claim `{item}`" for item in sorted(omitted_claim_ids))
        lines.extend(f"- unresolved ReportClaim `{item}`" for item in unresolved)
        lines.append("")
    if acquisition_requests:
        lines.extend(["## Pending evidence acquisition", ""])
        lines.extend(
            f"- `{item.acquisition_request_id}` for `{item.report_claim_id}`"
            for item in sorted(
                acquisition_requests,
                key=lambda item: item.acquisition_request_id,
            )
        )
        lines.append("")
    for citation in sorted(draft.citations, key=lambda item: item.citation_id):
        report_claim = next(
            item for item in draft.report_claims if item.claim_id == citation.claim_id
        )
        if report_claim.report_claim_id not in published_report_claim_ids:
            continue
        lines.append(
            f"[^{citation.citation_id}]: source={citation.source_id}; "
            f"evidence={citation.evidence_id}@{citation.evidence_revision}"
        )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")
