"""Phase 6 synthesis and bounded verification contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.claims import (
    CitationIntegrityIssue,
    CitationReference,
    ClaimEvidenceRelation,
    ClaimGraphSnapshot,
    ConflictCandidate,
    EdgeView,
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
from researchos.domain.provider_diagnostics import validate_provider_diagnostics
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


class FindingDisposition(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class VerificationTerminationReason(StrEnum):
    JUDGE_FINALIZED = "judge_finalized"
    MAX_ROUNDS = "max_rounds"
    MAX_FINDINGS = "max_findings"


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
    current_valid_edges: tuple[EdgeView, ...] = ()
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
            [
                self.synthesis_id,
                self.revision_number,
                self.parent_revision_id,
                self.canonical_content_hash,
            ],
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

    @model_validator(mode="after")
    def pins_match_type(self) -> RedCandidateFinding:
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


class RedResponse(ContractModel):
    findings: tuple[RedCandidateFinding, ...] = ()


class BlueAction(ContractModel):
    action: BlueActionType
    report_claim_id: SafeId
    finding_id: SafeId
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
    finding_id: SafeId
    claim_id: SafeId
    reason: ShortText


class JudgeDecision(ContractModel):
    report_claim_id: SafeId
    verdict: JudgeVerdict


class FindingDispositionDecision(ContractModel):
    finding_id: SafeId
    disposition: FindingDisposition


class JudgeResponse(ContractModel):
    draft_revision_id: SafeId
    action: JudgeRoundAction = JudgeRoundAction.FINALIZE
    continue_reason: JudgeContinueReason | None = None
    decisions: tuple[JudgeDecision, ...]
    finding_dispositions: tuple[FindingDispositionDecision, ...]

    @model_validator(mode="after")
    def continuation_is_explicit(self) -> JudgeResponse:
        if (self.action is JudgeRoundAction.CONTINUE) != (
            self.continue_reason is not None
        ):
            raise ValueError("CONTINUE requires exactly one continue reason")
        return self


class CitationAssignment(ContractModel):
    report_claim_id: SafeId
    citation: CitationReference
    edge_id: SafeId
    relation: ClaimEvidenceRelation


class VerificationRoundRecord(ContractModel):
    round_number: int = Field(ge=1)
    input_draft_revision_id: SafeId
    red_findings: tuple[RedFinding, ...]
    open_findings: tuple[RedFinding, ...]
    blue_actions: tuple[BlueAction, ...]
    output_draft_revision_id: SafeId
    judge_decisions: tuple[JudgeDecision, ...]
    finding_dispositions: tuple[FindingDispositionDecision, ...]
    judge_action: JudgeRoundAction
    continue_reason: JudgeContinueReason | None = None


class VerificationResult(ContractModel):
    verification_id: SafeId
    synthesis_id: SafeId
    run_id: SafeId
    run_revision: int = Field(ge=0)
    frozen_input_hash: Sha256
    claim_snapshot_hash: Sha256
    evidence_snapshot_hash: Sha256
    claim_store_revision: int = Field(ge=0)
    evidence_store_revision: int = Field(ge=0)
    selected_claim_ids: tuple[SafeId, ...]
    omitted_claim_ids: tuple[SafeId, ...]
    selected_evidence_ids: tuple[SafeId, ...]
    omitted_evidence_ids: tuple[SafeId, ...]
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
    citation_assignments: tuple[CitationAssignment, ...]
    round_records: tuple[VerificationRoundRecord, ...]
    citation_issues: tuple[CitationIntegrityIssue, ...]
    disposition: VerificationDisposition
    termination_reason: VerificationTerminationReason
    markdown_sha256: Sha256
    usage: RuntimeResourceAmount
    usage_certainty: UsageCertainty
    completed_at: datetime
    artifact_content_hash: Sha256

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
        if set(self.selected_claim_ids) & set(self.omitted_claim_ids):
            raise ValueError("selected and omitted claims overlap")
        if set(self.selected_evidence_ids) & set(self.omitted_evidence_ids):
            raise ValueError("selected and omitted evidence overlap")
        claims = {item.report_claim_id: item for item in self.final_draft.report_claims}
        citation_ids = {item.citation_id for item in self.final_draft.citations}
        assignment_ids = [
            item.citation.citation_id for item in self.citation_assignments
        ]
        if set(assignment_ids) != citation_ids or len(assignment_ids) != len(
            set(assignment_ids)
        ):
            raise ValueError("citation assignments must cover final citations exactly")
        if any(
            item.report_claim_id not in claims for item in self.citation_assignments
        ):
            raise ValueError("citation assignment references missing report claim")
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
        decision_ids = [item.report_claim_id for item in self.judge_decisions]
        if set(decision_ids) != set(claims) or len(decision_ids) != len(
            set(decision_ids)
        ):
            raise ValueError("final Judge decisions must cover report claims exactly")
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
        if tuple(item.round_number for item in self.round_records) != tuple(
            range(1, len(self.round_records) + 1)
        ):
            raise ValueError("round records must be continuous")
        if not self.round_records:
            raise ValueError("verification requires a completed Judge round")
        if len(ordered) != len(self.round_records) + 1:
            raise ValueError("draft and round lineage lengths differ")
        for index, record in enumerate(self.round_records):
            if record.input_draft_revision_id != ordered[index].draft_revision_id:
                raise ValueError("round input draft lineage differs")
            if record.output_draft_revision_id != ordered[index + 1].draft_revision_id:
                raise ValueError("round output draft lineage differs")
            record_findings = {item.finding_id for item in record.open_findings}
            disposition_ids = [item.finding_id for item in record.finding_dispositions]
            if set(disposition_ids) != record_findings or len(disposition_ids) != len(
                set(disposition_ids)
            ):
                raise ValueError("Judge must disposition round findings exactly once")
            action_ids = [item.finding_id for item in record.blue_actions]
            if set(action_ids) != record_findings or len(action_ids) != len(
                set(action_ids)
            ):
                raise ValueError("Blue must act on round findings exactly once")
            finding_claims = {
                item.finding_id: item.report_claim_id for item in record.open_findings
            }
            if any(
                item.report_claim_id != finding_claims[item.finding_id]
                for item in record.blue_actions
            ):
                raise ValueError("Blue action and finding Claim differ")
            output_claim_ids = {
                item.report_claim_id for item in ordered[index + 1].report_claims
            }
            round_decision_ids = [
                item.report_claim_id for item in record.judge_decisions
            ]
            if set(round_decision_ids) != output_claim_ids or len(
                round_decision_ids
            ) != len(set(round_decision_ids)):
                raise ValueError("round Judge decisions must cover output Claims")
        if self.judge_decisions != self.round_records[-1].judge_decisions:
            raise ValueError("final Judge decisions differ from final round")
        replayed_resolved: set[str] = set()
        known_findings: set[str] = set()
        for record in self.round_records:
            for finding in record.red_findings:
                known_findings.add(finding.finding_id)
                replayed_resolved.discard(finding.finding_id)
            for disposition in record.finding_dispositions:
                if disposition.disposition is FindingDisposition.RESOLVED:
                    replayed_resolved.add(disposition.finding_id)
                else:
                    replayed_resolved.discard(disposition.finding_id)
        if known_findings != finding_ids:
            raise ValueError("round finding lineage differs from cumulative findings")
        if replayed_resolved != set(self.resolved_finding_ids):
            raise ValueError("resolved findings differ from Judge dispositions")
        if self.termination_reason is VerificationTerminationReason.JUDGE_FINALIZED:
            if self.round_records[-1].judge_action is not JudgeRoundAction.FINALIZE:
                raise ValueError("JUDGE_FINALIZED requires Judge FINALIZE")
        elif (
            self.termination_reason is VerificationTerminationReason.MAX_ROUNDS
            and self.round_records[-1].judge_action is not JudgeRoundAction.CONTINUE
        ):
            raise ValueError("bounded termination requires Judge CONTINUE")
        if (
            self.termination_reason
            in {
                VerificationTerminationReason.MAX_ROUNDS,
                VerificationTerminationReason.MAX_FINDINGS,
            }
            and self.disposition is VerificationDisposition.VERIFIED
        ):
            raise ValueError("bounded termination cannot be VERIFIED")
        expected_artifact_hash = stable_hash(
            self.model_dump(mode="json", exclude={"artifact_content_hash"})
        )
        if self.artifact_content_hash != expected_artifact_hash:
            raise ValueError("verification artifact content hash differs")
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
    provider_diagnostics: dict[str, object] | None = Field(default=None, exclude=True)

    @field_validator("provider_diagnostics")
    @classmethod
    def provider_diagnostics_must_be_safe(cls, value: dict[str, object] | None):
        return validate_provider_diagnostics(value)
