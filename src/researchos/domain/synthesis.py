"""Phase 6 synthesis and bounded verification contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.claims import (
    CitationIntegrityIssue,
    CitationReference,
    ClaimGraphSnapshot,
    ConflictCandidate,
)
from researchos.domain.contracts import (
    ContractModel,
    SafeId,
    Sha256,
    _require_aware,
    model_sha256,
)
from researchos.domain.evidence import EvidenceStoreSnapshot
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty

SYNTHESIS_SCHEMA_VERSION = 1
Prose = Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class VerificationRole(StrEnum):
    SYNTHESIZER = "synthesizer"
    RED = "red"
    BLUE = "blue"
    JUDGE = "judge"


class FindingType(StrEnum):
    UNSUPPORTED_CLAIM = "unsupported_claim"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    CITATION_GAP = "citation_gap"
    OVERCLAIM = "overclaim"


class BlueActionType(StrEnum):
    KEEP = "keep"
    QUALIFY = "qualify"
    REMOVE = "remove"
    ADD_EXISTING_CITATION = "add_existing_citation"
    REQUEST_EVIDENCE = "request_evidence"


class PublicationState(StrEnum):
    INCLUDED = "included"
    REMOVED = "removed"


class StructuralSupport(StrEnum):
    STRUCTURALLY_SUPPORTED = "structurally_supported"
    CONFLICTED = "conflicted"
    UNSUPPORTED = "unsupported"


class JudgeVerdict(StrEnum):
    SUPPORTED = "supported"
    QUALIFIED = "qualified"
    REJECTED_AS_UNSUPPORTED = "rejected_as_unsupported"
    UNRESOLVED = "unresolved"


class JudgeRoundAction(StrEnum):
    FINALIZE = "finalize"
    CONTINUE = "continue"


class JudgeContinueReason(StrEnum):
    OPEN_FINDINGS = "open_findings"
    STRUCTURAL_CONFLICT = "structural_conflict"


class VerificationDisposition(StrEnum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class VerificationPolicy(ContractModel):
    max_rounds: int = Field(default=2, ge=1, le=10)
    max_claims: int = Field(default=100, ge=1, le=1_000)
    max_findings: int = Field(default=100, ge=1, le=2_000)
    max_evidence_per_claim: int = Field(default=8, ge=1, le=50)
    max_sections: int = Field(default=20, ge=1, le=100)
    max_report_bytes: int = Field(default=1_000_000, ge=1)
    max_model_context_bytes: int = Field(default=1_000_000, ge=1)
    max_model_response_bytes: int = Field(default=256_000, ge=1)
    max_verification_artifact_bytes: int = Field(default=5_000_000, ge=1)


class SnapshotFingerprint(ContractModel):
    run_id: SafeId
    store_revision: int = Field(ge=0)
    snapshot_hash: Sha256


class FrozenVerificationInput(ContractModel):
    run_id: SafeId
    run_revision: int = Field(ge=0)
    claim_snapshot: ClaimGraphSnapshot
    evidence_snapshot: EvidenceStoreSnapshot
    claim_snapshot_hash: Sha256
    evidence_snapshot_hash: Sha256
    selected_claim_ids: tuple[SafeId, ...]
    omitted_claim_ids: tuple[SafeId, ...] = ()
    selected_evidence_ids: tuple[SafeId, ...] = ()
    omitted_evidence_ids: tuple[SafeId, ...] = ()
    allowed_citations: tuple[CitationReference, ...] = ()
    conflict_candidates: tuple[ConflictCandidate, ...] = ()
    frozen_input_hash: Sha256

    @model_validator(mode="after")
    def references_one_run(self) -> FrozenVerificationInput:
        if self.claim_snapshot.run_id != self.run_id:
            raise ValueError("claim snapshot belongs to another run")
        if self.evidence_snapshot.run_id != self.run_id:
            raise ValueError("evidence snapshot belongs to another run")
        for values in (
            self.selected_claim_ids,
            self.omitted_claim_ids,
            self.selected_evidence_ids,
            self.omitted_evidence_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("frozen input IDs must be unique")
        if set(self.selected_claim_ids) & set(self.omitted_claim_ids):
            raise ValueError("claim cannot be selected and omitted")
        if set(self.selected_evidence_ids) & set(self.omitted_evidence_ids):
            raise ValueError("evidence cannot be selected and omitted")
        if self.claim_snapshot_hash != model_sha256(self.claim_snapshot):
            raise ValueError("claim snapshot hash differs")
        if self.evidence_snapshot_hash != model_sha256(self.evidence_snapshot):
            raise ValueError("evidence snapshot hash differs")
        return self


class SynthesisClaim(ContractModel):
    claim_id: SafeId
    claim_revision: int = Field(ge=1)
    section_ordinal: int = Field(ge=1)
    prose: Prose
    evidence_ids: tuple[SafeId, ...] = ()


class SynthesisCandidate(ContractModel):
    section_titles: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=200)], ...
    ]
    claims: tuple[SynthesisClaim, ...]


class ReportClaim(ContractModel):
    report_claim_id: SafeId
    claim_id: SafeId
    claim_revision: int = Field(ge=1)
    section_id: SafeId
    prose: Prose
    publication_state: PublicationState = PublicationState.INCLUDED
    structural_support: StructuralSupport


class ReportSection(ContractModel):
    section_id: SafeId
    ordinal: int = Field(ge=1)
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    report_claim_ids: tuple[SafeId, ...] = ()


class ReportDraftRevision(ContractModel):
    draft_revision_id: SafeId
    synthesis_id: SafeId
    revision_number: int = Field(ge=1)
    parent_revision_id: SafeId | None = None
    sections: tuple[ReportSection, ...]
    report_claims: tuple[ReportClaim, ...]
    citations: tuple[CitationReference, ...]
    created_by_round: int = Field(ge=0)
    canonical_content_hash: Sha256

    @model_validator(mode="after")
    def draft_is_consistent(self) -> ReportDraftRevision:
        if (self.revision_number == 1) != (self.parent_revision_id is None):
            raise ValueError("only the first draft has no parent")
        section_ids = {item.section_id for item in self.sections}
        report_ids = [item.report_claim_id for item in self.report_claims]
        if len(report_ids) != len(set(report_ids)):
            raise ValueError("duplicate report claim identity")
        if any(item.section_id not in section_ids for item in self.report_claims):
            raise ValueError("report claim references missing section")
        memberships = [
            report_claim_id
            for section in self.sections
            for report_claim_id in section.report_claim_ids
        ]
        if len(memberships) != len(set(memberships)):
            raise ValueError("report claim has duplicate section membership")
        included = {
            item.report_claim_id
            for item in self.report_claims
            if item.publication_state is PublicationState.INCLUDED
        }
        if set(memberships) != included:
            raise ValueError("section membership must contain exactly included claims")
        section_by_id = {item.section_id: item for item in self.sections}
        if any(
            item.publication_state is PublicationState.INCLUDED
            and item.report_claim_id
            not in section_by_id[item.section_id].report_claim_ids
            for item in self.report_claims
        ):
            raise ValueError("report claim section membership differs")
        for section in self.sections:
            if section.section_id != stable_id(
                "sec", [self.synthesis_id, section.ordinal]
            ):
                raise ValueError("section identity differs")
        claims_by_claim_id = {item.claim_id: item for item in self.report_claims}
        if len(claims_by_claim_id) != len(self.report_claims):
            raise ValueError("one report claim is allowed per selected claim")
        for claim in self.report_claims:
            if claim.report_claim_id != stable_id(
                "rcl", [self.synthesis_id, claim.claim_id, claim.claim_revision]
            ):
                raise ValueError("report claim identity differs")
        citation_ids = [item.citation_id for item in self.citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("duplicate citation identity")
        for citation in self.citations:
            claim = claims_by_claim_id.get(citation.claim_id)
            if claim is None or claim.claim_revision != citation.claim_revision:
                raise ValueError("citation is not bound to exactly one report claim")
            if citation.citation_id != stable_id(
                "cit",
                [
                    claim.report_claim_id,
                    citation.evidence_id,
                    citation.evidence_revision,
                    citation.source_id,
                    citation.expected_evidence_content_hash,
                ],
            ):
                raise ValueError("citation identity differs")
        content = {
            "sections": [item.model_dump(mode="json") for item in self.sections],
            "claims": [item.model_dump(mode="json") for item in self.report_claims],
            "citations": [item.model_dump(mode="json") for item in self.citations],
        }
        if self.canonical_content_hash != stable_hash(content):
            raise ValueError("draft content hash differs")
        if self.draft_revision_id != stable_id(
            "draft",
            [self.synthesis_id, self.revision_number, self.canonical_content_hash],
        ):
            raise ValueError("draft revision identity differs")
        return self


class RedFinding(ContractModel):
    finding_id: SafeId
    finding_type: FindingType
    report_claim_id: SafeId
    rationale: ShortText
    citation_id: SafeId | None = None
    evidence_id: SafeId | None = None

    @model_validator(mode="after")
    def pins_match_type(self) -> RedFinding:
        if (
            self.finding_type is FindingType.CONTRADICTORY_EVIDENCE
            and not self.evidence_id
        ):
            raise ValueError("contradictory evidence finding requires evidence_id")
        if (
            self.finding_type is FindingType.CITATION_GAP
            and self.citation_id is not None
        ):
            raise ValueError("citation gap must not pin a citation")
        return self


class RedCandidateFinding(ContractModel):
    finding_type: FindingType
    report_claim_id: SafeId
    rationale: ShortText
    citation_id: SafeId | None = None
    evidence_id: SafeId | None = None


class RedResponse(ContractModel):
    findings: tuple[RedCandidateFinding, ...] = ()


class BlueAction(ContractModel):
    action: BlueActionType
    report_claim_id: SafeId
    finding_id: SafeId | None = None
    qualified_prose: Prose | None = None
    evidence_id: SafeId | None = None
    reason: ShortText | None = None

    @model_validator(mode="after")
    def action_fields_match(self) -> BlueAction:
        if self.action is BlueActionType.QUALIFY and self.qualified_prose is None:
            raise ValueError("qualify requires qualified prose")
        if (
            self.action is BlueActionType.ADD_EXISTING_CITATION
            and self.evidence_id is None
        ):
            raise ValueError("add citation requires evidence_id")
        if self.action is BlueActionType.REQUEST_EVIDENCE and self.reason is None:
            raise ValueError("evidence request requires reason")
        return self


class BlueResponse(ContractModel):
    actions: tuple[BlueAction, ...] = ()


class AcquisitionRequest(ContractModel):
    acquisition_request_id: SafeId
    report_claim_id: SafeId
    finding_id: SafeId | None = None
    claim_id: SafeId
    reason: ShortText


class JudgeDecision(ContractModel):
    report_claim_id: SafeId
    verdict: JudgeVerdict


class JudgeResponse(ContractModel):
    draft_revision_id: SafeId
    action: JudgeRoundAction = JudgeRoundAction.FINALIZE
    continue_reason: JudgeContinueReason | None = None
    decisions: tuple[JudgeDecision, ...]

    @model_validator(mode="after")
    def continuation_is_explicit(self) -> JudgeResponse:
        if (self.action is JudgeRoundAction.CONTINUE) != (
            self.continue_reason is not None
        ):
            raise ValueError("CONTINUE requires exactly one continue reason")
        return self


class VerificationResult(ContractModel):
    verification_id: SafeId
    synthesis_id: SafeId
    run_id: SafeId
    run_revision: int = Field(ge=0)
    frozen_input_hash: Sha256
    claim_snapshot_hash: Sha256
    evidence_snapshot_hash: Sha256
    policy_hash: Sha256
    model_bundle_hash: Sha256
    supersedes_verification_id: SafeId | None = None
    draft_revisions: tuple[ReportDraftRevision, ...]
    final_draft_revision_id: SafeId
    final_draft: ReportDraftRevision
    findings: tuple[RedFinding, ...]
    resolved_finding_ids: tuple[SafeId, ...] = ()
    acquisition_requests: tuple[AcquisitionRequest, ...]
    judge_decisions: tuple[JudgeDecision, ...]
    citation_issues: tuple[CitationIntegrityIssue, ...]
    disposition: VerificationDisposition
    markdown_sha256: Sha256
    usage: RuntimeResourceAmount
    usage_certainty: UsageCertainty
    completed_at: datetime

    _aware = field_validator("completed_at")(_require_aware)

    @model_validator(mode="after")
    def supported_means_structural_support(self) -> VerificationResult:
        expected_verification_id = stable_id(
            "ver",
            [
                self.run_id,
                self.run_revision,
                self.claim_snapshot_hash,
                self.evidence_snapshot_hash,
                self.policy_hash,
                self.model_bundle_hash,
            ],
        )
        if self.verification_id != expected_verification_id:
            raise ValueError("verification identity differs")
        if self.synthesis_id != stable_id("syn", [self.verification_id, "initial"]):
            raise ValueError("synthesis identity differs")
        if self.final_draft.synthesis_id != self.synthesis_id:
            raise ValueError("draft belongs to another synthesis")
        if not self.draft_revisions:
            raise ValueError("verification requires draft lineage")
        ordered = tuple(
            sorted(self.draft_revisions, key=lambda item: item.revision_number)
        )
        if tuple(item.revision_number for item in ordered) != tuple(
            range(1, len(ordered) + 1)
        ):
            raise ValueError("draft revision lineage must be continuous")
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.parent_revision_id != previous.draft_revision_id:
                raise ValueError("draft revision parent differs")
        if self.final_draft_revision_id != ordered[-1].draft_revision_id:
            raise ValueError("final draft revision ID differs")
        if self.final_draft != ordered[-1]:
            raise ValueError("final draft is not the final lineage revision")
        claims = {item.report_claim_id: item for item in self.final_draft.report_claims}
        for decision in self.judge_decisions:
            claim = claims.get(decision.report_claim_id)
            if claim is None:
                raise ValueError("judge decision references missing report claim")
            if (
                decision.verdict is JudgeVerdict.SUPPORTED
                and claim.structural_support
                is not StructuralSupport.STRUCTURALLY_SUPPORTED
            ):
                raise ValueError("SUPPORTED requires STRUCTURALLY_SUPPORTED")
        for finding in self.findings:
            if finding.report_claim_id not in claims:
                raise ValueError("finding references missing report claim")
            expected = stable_id(
                "find",
                [
                    self.verification_id,
                    finding.finding_type.value,
                    finding.report_claim_id,
                    finding.citation_id,
                    finding.evidence_id,
                ],
            )
            if finding.finding_id != expected:
                raise ValueError("finding identity differs")
        finding_ids = {item.finding_id for item in self.findings}
        if not set(self.resolved_finding_ids).issubset(finding_ids):
            raise ValueError("resolved finding identity is absent")
        for request in self.acquisition_requests:
            expected = stable_id(
                "acq",
                [
                    self.verification_id,
                    request.report_claim_id,
                    request.finding_id,
                    request.claim_id,
                ],
            )
            if request.acquisition_request_id != expected:
                raise ValueError("acquisition request identity differs")
        return self


class VerificationModelRequest(ContractModel):
    verification_id: SafeId
    role: VerificationRole
    round_number: int = Field(ge=0)
    draft_revision_id: SafeId
    context: dict[str, object]


class VerificationModelResponse(ContractModel):
    raw_bytes: bytes
    usage: RuntimeResourceAmount = Field(default_factory=RuntimeResourceAmount)
    usage_certainty: UsageCertainty = UsageCertainty.EXACT
    model_id: SafeId
    mode: Literal["mock", "real"]
    role: VerificationRole
    round_number: int = Field(ge=0)
    draft_revision_id: SafeId
