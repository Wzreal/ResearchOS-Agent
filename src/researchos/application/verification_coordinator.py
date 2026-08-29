"""Bounded Red/Blue/Judge coordination without scheduler or persistence ownership."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from researchos.application.errors import (
    VerificationCancelled,
    VerificationContractError,
    VerificationDeadlineExceeded,
    VerificationModelFailure,
    VerificationUsageUncertain,
)
from researchos.application.verification_publisher import is_publishable
from researchos.domain.claims import (
    CitationIntegrityIssue,
    CitationReference,
    CitationSeverity,
    ClaimEvidenceRelation,
)
from researchos.domain.contracts import (
    OperatingMode,
    TraceEventType,
    canonical_json_bytes,
)
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.synthesis import (
    AcquisitionRequest,
    BlueActionType,
    BlueResponse,
    FrozenVerificationInput,
    JudgeContinueReason,
    JudgeDecision,
    JudgeResponse,
    JudgeRoundAction,
    JudgeVerdict,
    PublicationState,
    RedFinding,
    RedResponse,
    ReportClaim,
    ReportDraftRevision,
    ReportSection,
    StructuralSupport,
    SynthesisCandidate,
    VerificationDisposition,
    VerificationModelRequest,
    VerificationPolicy,
    VerificationRole,
)
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.runtime import CancellationSignal
from researchos.interfaces.verification import VerificationModel

T = TypeVar("T", bound=BaseModel)


class VerificationCoordinator:
    def __init__(self, *, model: VerificationModel, clock: Clock) -> None:
        self._model = model
        self._clock = clock
        self._usage = RuntimeResourceAmount()
        self._certainty = UsageCertainty.EXACT
        self._expected_mode = OperatingMode.MOCK
        self._event_callback: (
            Callable[[TraceEventType, dict[str, object]], None] | None
        ) = None

    @property
    def model_bundle_hash(self) -> str:
        return self._model.model_bundle_hash

    async def execute(
        self,
        *,
        verification_id: str,
        frozen: FrozenVerificationInput,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        deadline: datetime | None,
        cancellation: CancellationSignal,
        expected_mode: OperatingMode = OperatingMode.MOCK,
        event_callback: Callable[[TraceEventType, dict[str, object]], None]
        | None = None,
    ) -> tuple[
        ReportDraftRevision,
        tuple[ReportDraftRevision, ...],
        tuple[RedFinding, ...],
        tuple[str, ...],
        tuple[AcquisitionRequest, ...],
        tuple[JudgeDecision, ...],
        RuntimeResourceAmount,
        UsageCertainty,
        bool,
    ]:
        self._usage = RuntimeResourceAmount()
        self._certainty = UsageCertainty.EXACT
        self._expected_mode = expected_mode
        self._event_callback = event_callback
        synthesis_id = stable_id("syn", [verification_id, "initial"])
        self._notify(TraceEventType.SYNTHESIS_STARTED, {"synthesis_id": synthesis_id})
        candidate = await self._call(
            SynthesisCandidate,
            VerificationModelRequest(
                verification_id=verification_id,
                role=VerificationRole.SYNTHESIZER,
                round_number=0,
                draft_revision_id="draft_pending",
                context=self._base_context(frozen),
            ),
            policy,
            hard_limits,
            deadline,
            cancellation,
        )
        draft = self._initial_draft(synthesis_id, candidate, frozen, policy)
        draft_revisions = [draft]
        self._notify(
            TraceEventType.SYNTHESIS_VALIDATED,
            {
                "synthesis_id": synthesis_id,
                "draft_revision_id": draft.draft_revision_id,
                "report_claim_count": len(draft.report_claims),
            },
        )
        findings_by_id: dict[str, RedFinding] = {}
        acquisitions: dict[str, AcquisitionRequest] = {}
        resolved_finding_ids: set[str] = set()
        bounds_exhausted = False
        decisions: tuple[JudgeDecision, ...] = ()
        for round_number in range(1, policy.max_rounds + 1):
            red = await self._call(
                RedResponse,
                self._request(
                    verification_id, VerificationRole.RED, round_number, draft, frozen
                ),
                policy,
                hard_limits,
                deadline,
                cancellation,
            )
            current_findings = self._validate_findings(
                verification_id, red, draft, frozen
            )
            self._notify(
                TraceEventType.VERIFICATION_RED_COMPLETED,
                {"round": round_number, "finding_count": len(current_findings)},
            )
            for finding in current_findings:
                findings_by_id.setdefault(finding.finding_id, finding)
            if len(findings_by_id) > policy.max_findings:
                bounds_exhausted = True
                findings_by_id = dict(
                    sorted(findings_by_id.items())[: policy.max_findings]
                )
            blue = await self._call(
                BlueResponse,
                self._request(
                    verification_id,
                    VerificationRole.BLUE,
                    round_number,
                    draft,
                    frozen,
                    findings=tuple(findings_by_id.values()),
                ),
                policy,
                hard_limits,
                deadline,
                cancellation,
            )
            self._notify(
                TraceEventType.VERIFICATION_BLUE_COMPLETED,
                {"round": round_number, "action_count": len(blue.actions)},
            )
            draft, new_acquisitions, newly_resolved = self._apply_blue(
                verification_id,
                draft,
                blue,
                frozen,
                tuple(findings_by_id.values()),
                round_number,
            )
            draft_revisions.append(draft)
            self._notify(
                TraceEventType.VERIFICATION_DRAFT_REVISED,
                {
                    "round": round_number,
                    "draft_revision_id": draft.draft_revision_id,
                },
            )
            for item in new_acquisitions:
                acquisitions.setdefault(item.acquisition_request_id, item)
            resolved_finding_ids.update(newly_resolved)
            judge = await self._call(
                JudgeResponse,
                self._request(
                    verification_id,
                    VerificationRole.JUDGE,
                    round_number,
                    draft,
                    frozen,
                    findings=tuple(findings_by_id.values()),
                ),
                policy,
                hard_limits,
                deadline,
                cancellation,
            )
            decisions = self._validate_judge(judge, draft)
            self._notify(
                TraceEventType.VERIFICATION_JUDGE_COMPLETED,
                {
                    "round": round_number,
                    "draft_revision_id": draft.draft_revision_id,
                    "decision_count": len(decisions),
                    "action": judge.action.value,
                },
            )
            self._notify(
                TraceEventType.VERIFICATION_ROUND_COMPLETED,
                {"round": round_number, "draft_revision_id": draft.draft_revision_id},
            )
            if judge.action is JudgeRoundAction.FINALIZE:
                break
            self._validate_continue(
                judge,
                draft,
                tuple(findings_by_id.values()),
                tuple(resolved_finding_ids),
            )
            if round_number == policy.max_rounds:
                bounds_exhausted = True
        return (
            draft,
            tuple(draft_revisions),
            tuple(sorted(findings_by_id.values(), key=lambda item: item.finding_id)),
            tuple(sorted(resolved_finding_ids)),
            tuple(
                sorted(
                    acquisitions.values(), key=lambda item: item.acquisition_request_id
                )
            ),
            decisions,
            self._usage,
            self._certainty,
            bounds_exhausted,
        )

    async def _call(
        self,
        response_type: type[T],
        request: VerificationModelRequest,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        deadline: datetime | None,
        cancellation: CancellationSignal,
    ) -> T:
        encoded_request = canonical_json_bytes(request)
        if len(encoded_request) > policy.max_model_context_bytes:
            raise VerificationContractError("model request exceeds context byte limit")
        if cancellation.cancelled:
            raise VerificationCancelled("verification cancelled")
        timeout = None
        if deadline is not None:
            timeout = (deadline - self._clock.now()).total_seconds()
            if timeout <= 0:
                raise VerificationDeadlineExceeded("verification deadline elapsed")
        model_task = asyncio.create_task(self._model.invoke(request, cancellation))
        cancel_task = asyncio.create_task(cancellation.wait())
        done, _ = await asyncio.wait(
            {model_task, cancel_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if model_task not in done:
            model_task.cancel()
            await asyncio.gather(model_task, return_exceptions=True)
            if cancel_task in done or cancellation.cancelled:
                raise VerificationCancelled("verification cancelled")
            cancel_task.cancel()
            raise VerificationDeadlineExceeded("verification model call timed out")
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        try:
            response = await model_task
        except (VerificationCancelled, VerificationDeadlineExceeded):
            raise
        except Exception as exc:
            raise VerificationModelFailure("verification provider failed") from exc
        if len(response.raw_bytes) > policy.max_model_response_bytes:
            raise VerificationContractError("model response exceeds byte limit")
        if response.mode != self._expected_mode.value:
            raise VerificationContractError("verification model mode mismatch")
        if (
            response.role is not request.role
            or response.round_number != request.round_number
            or response.draft_revision_id != request.draft_revision_id
        ):
            raise VerificationContractError("verification response identity mismatch")
        self._usage = self._usage.plus(response.usage)
        if response.usage_certainty is UsageCertainty.UNKNOWN:
            self._certainty = UsageCertainty.UNKNOWN
        elif (
            response.usage_certainty is UsageCertainty.UPPER_BOUND
            and self._certainty is UsageCertainty.EXACT
        ):
            self._certainty = UsageCertainty.UPPER_BOUND
        if not self._usage.fits_within(hard_limits):
            raise VerificationContractError("verification hard limits exceeded")
        if (
            response.usage_certainty is UsageCertainty.UNKNOWN
            and request.role is not VerificationRole.JUDGE
        ):
            raise VerificationUsageUncertain(
                "unknown usage stops further verification model calls"
            )
        try:
            payload = json.loads(response.raw_bytes)
            return response_type.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise VerificationModelFailure(
                "verification model response is malformed"
            ) from exc

    def _notify(
        self, event_type: TraceEventType, attributes: dict[str, object]
    ) -> None:
        if self._event_callback is not None:
            self._event_callback(event_type, attributes)

    @staticmethod
    def _base_context(frozen: FrozenVerificationInput) -> dict[str, Any]:
        claim_revisions = {
            (item.claim_id, item.revision): item
            for item in frozen.claim_snapshot.claim_revisions
        }
        claim_records = {item.claim_id: item for item in frozen.claim_snapshot.claims}
        evidence_records = {
            item.evidence_id: item for item in frozen.evidence_snapshot.evidence
        }
        evidence_revisions = {
            (item.evidence_id, item.revision): item
            for item in frozen.evidence_snapshot.revisions
        }
        return {
            "frozen_input_hash": frozen.frozen_input_hash,
            "claims": [
                claim_revisions[
                    (claim_id, claim_records[claim_id].current_revision)
                ].model_dump(mode="json")
                for claim_id in frozen.selected_claim_ids
            ],
            "evidence": [
                evidence_revisions[
                    (evidence_id, evidence_records[evidence_id].current_revision)
                ].model_dump(mode="json")
                for evidence_id in frozen.selected_evidence_ids
            ],
            "citation_allowlist": [
                item.model_dump(mode="json") for item in frozen.allowed_citations
            ],
            "conflicts": [
                item.model_dump(mode="json") for item in frozen.conflict_candidates
            ],
            "omitted_claim_ids": list(frozen.omitted_claim_ids),
            "omitted_evidence_ids": list(frozen.omitted_evidence_ids),
        }

    def _request(
        self,
        verification_id: str,
        role: VerificationRole,
        round_number: int,
        draft: ReportDraftRevision,
        frozen: FrozenVerificationInput,
        *,
        findings: tuple[RedFinding, ...] = (),
    ) -> VerificationModelRequest:
        return VerificationModelRequest(
            verification_id=verification_id,
            role=role,
            round_number=round_number,
            draft_revision_id=draft.draft_revision_id,
            context={
                **self._base_context(frozen),
                "draft": draft.model_dump(mode="json"),
                "findings": [item.model_dump(mode="json") for item in findings],
            },
        )

    @staticmethod
    def _support(claim_id: str, frozen: FrozenVerificationInput) -> StructuralSupport:
        relations = set()
        selected = set(frozen.selected_evidence_ids)
        edge_by_id = {item.edge_id: item for item in frozen.claim_snapshot.edges}
        for revision in frozen.claim_snapshot.edge_revisions:
            edge = edge_by_id[revision.edge_id]
            if (
                edge.current_revision != revision.revision
                or edge.lifecycle is not RecordLifecycle.ACTIVE
                or edge.claim_id != claim_id
                or edge.evidence_id not in selected
            ):
                continue
            relations.add(revision.relation)
        if (
            ClaimEvidenceRelation.SUPPORTS in relations
            and ClaimEvidenceRelation.CONTRADICTS not in relations
        ):
            return StructuralSupport.STRUCTURALLY_SUPPORTED
        if (
            ClaimEvidenceRelation.SUPPORTS in relations
            and ClaimEvidenceRelation.CONTRADICTS in relations
        ):
            return StructuralSupport.CONFLICTED
        return StructuralSupport.UNSUPPORTED

    def _initial_draft(
        self,
        synthesis_id: str,
        candidate: SynthesisCandidate,
        frozen: FrozenVerificationInput,
        policy: VerificationPolicy,
    ) -> ReportDraftRevision:
        if len(candidate.section_titles) > policy.max_sections:
            raise VerificationContractError("synthesis exceeds section limit")
        expected = set(frozen.selected_claim_ids)
        actual = [item.claim_id for item in candidate.claims]
        if set(actual) != expected or len(actual) != len(set(actual)):
            raise VerificationContractError(
                "synthesis must cover selected claims exactly once"
            )
        sections = tuple(
            ReportSection(
                section_id=stable_id("sec", [synthesis_id, ordinal]),
                ordinal=ordinal,
                title=title,
            )
            for ordinal, title in enumerate(candidate.section_titles, 1)
        )
        section_by_ordinal = {item.ordinal: item for item in sections}
        current_claims = {item.claim_id: item for item in frozen.claim_snapshot.claims}
        allowed = {
            (item.claim_id, item.evidence_id): item for item in frozen.allowed_citations
        }
        report_claims: list[ReportClaim] = []
        citations: list[CitationReference] = []
        for item in candidate.claims:
            record = current_claims[item.claim_id]
            if (
                item.claim_revision != record.current_revision
                or item.section_ordinal not in section_by_ordinal
            ):
                raise VerificationContractError("synthesis claim pin is invalid")
            report_claim_id = stable_id(
                "rcl", [synthesis_id, item.claim_id, item.claim_revision]
            )
            report_claims.append(
                ReportClaim(
                    report_claim_id=report_claim_id,
                    claim_id=item.claim_id,
                    claim_revision=item.claim_revision,
                    section_id=section_by_ordinal[item.section_ordinal].section_id,
                    prose=item.prose,
                    structural_support=self._support(item.claim_id, frozen),
                )
            )
            for evidence_id in sorted(set(item.evidence_ids)):
                pin = allowed.get((item.claim_id, evidence_id))
                if pin is None:
                    raise VerificationContractError(
                        "synthesis citation is outside frozen input"
                    )
                citations.append(
                    pin.model_copy(
                        update={
                            "citation_id": stable_id(
                                "cit",
                                [
                                    report_claim_id,
                                    evidence_id,
                                    pin.evidence_revision,
                                    pin.source_id,
                                    pin.expected_evidence_content_hash,
                                ],
                            )
                        }
                    )
                )
        return self._make_draft(
            synthesis_id, 1, None, sections, tuple(report_claims), tuple(citations), 0
        )

    @staticmethod
    def _make_draft(
        synthesis_id: str,
        revision: int,
        parent: str | None,
        sections: tuple[ReportSection, ...],
        claims: tuple[ReportClaim, ...],
        citations: tuple[CitationReference, ...],
        round_number: int,
    ) -> ReportDraftRevision:
        claims = tuple(sorted(claims, key=lambda item: item.report_claim_id))
        included_by_section: dict[str, list[str]] = {}
        for claim in claims:
            if claim.publication_state is PublicationState.INCLUDED:
                included_by_section.setdefault(claim.section_id, []).append(
                    claim.report_claim_id
                )
        sections = tuple(
            sorted(
                (
                    item.model_copy(
                        update={
                            "report_claim_ids": tuple(
                                sorted(included_by_section.get(item.section_id, ()))
                            )
                        }
                    )
                    for item in sections
                ),
                key=lambda item: (item.ordinal, item.section_id),
            )
        )
        citations = tuple(sorted(citations, key=lambda item: item.citation_id))
        content = {
            "sections": [item.model_dump(mode="json") for item in sections],
            "claims": [item.model_dump(mode="json") for item in claims],
            "citations": [item.model_dump(mode="json") for item in citations],
        }
        digest = stable_hash(content)
        return ReportDraftRevision(
            draft_revision_id=stable_id("draft", [synthesis_id, revision, digest]),
            synthesis_id=synthesis_id,
            revision_number=revision,
            parent_revision_id=parent,
            sections=sections,
            report_claims=claims,
            citations=citations,
            created_by_round=round_number,
            canonical_content_hash=digest,
        )

    @staticmethod
    def _validate_findings(
        verification_id: str,
        response: RedResponse,
        draft: ReportDraftRevision,
        frozen: FrozenVerificationInput,
    ) -> tuple[RedFinding, ...]:
        report_ids = {item.report_claim_id for item in draft.report_claims}
        report_claims = {item.report_claim_id: item for item in draft.report_claims}
        citations = {item.citation_id: item for item in draft.citations}
        citation_ids = set(citations)
        evidence_ids = set(frozen.selected_evidence_ids)
        findings: list[RedFinding] = []
        for item in response.findings:
            if item.report_claim_id not in report_ids:
                raise VerificationContractError(
                    "finding report claim is outside frozen draft"
                )
            if item.citation_id is not None and item.citation_id not in citation_ids:
                raise VerificationContractError(
                    "finding citation is outside frozen input"
                )
            if item.citation_id is not None and (
                citations[item.citation_id].claim_id
                != report_claims[item.report_claim_id].claim_id
            ):
                raise VerificationContractError(
                    "finding citation belongs to another report claim"
                )
            if item.evidence_id is not None and item.evidence_id not in evidence_ids:
                raise VerificationContractError(
                    "finding evidence is outside frozen input"
                )
            findings.append(
                RedFinding(
                    **item.model_dump(),
                    finding_id=stable_id(
                        "find",
                        [
                            verification_id,
                            item.finding_type.value,
                            item.report_claim_id,
                            item.citation_id,
                            item.evidence_id,
                        ],
                    ),
                )
            )
        return tuple(findings)

    def _apply_blue(
        self,
        verification_id: str,
        draft: ReportDraftRevision,
        response: BlueResponse,
        frozen: FrozenVerificationInput,
        findings: tuple[RedFinding, ...],
        round_number: int,
    ) -> tuple[ReportDraftRevision, tuple[AcquisitionRequest, ...], tuple[str, ...]]:
        claims = {item.report_claim_id: item for item in draft.report_claims}
        finding_ids = {item.finding_id for item in findings}
        allowed = {
            (item.claim_id, item.evidence_id): item for item in frozen.allowed_citations
        }
        citations = list(draft.citations)
        acquisitions: list[AcquisitionRequest] = []
        resolved: set[str] = set()
        action_claim_ids = [item.report_claim_id for item in response.actions]
        if len(action_claim_ids) != len(set(action_claim_ids)):
            raise VerificationContractError("conflicting Blue actions for report claim")
        for action in sorted(
            response.actions, key=lambda item: (item.report_claim_id, item.action.value)
        ):
            claim = claims.get(action.report_claim_id)
            if claim is None or (
                action.finding_id is not None and action.finding_id not in finding_ids
            ):
                raise VerificationContractError(
                    "Blue action references unknown frozen identity"
                )
            if action.action is BlueActionType.QUALIFY:
                claims[action.report_claim_id] = claim.model_copy(
                    update={"prose": action.qualified_prose}
                )
            elif action.action is BlueActionType.REMOVE:
                claims[action.report_claim_id] = claim.model_copy(
                    update={"publication_state": PublicationState.REMOVED}
                )
                citations = [
                    item for item in citations if item.claim_id != claim.claim_id
                ]
            elif action.action is BlueActionType.ADD_EXISTING_CITATION:
                pin = allowed.get((claim.claim_id, action.evidence_id))
                if pin is None:
                    raise VerificationContractError(
                        "Blue citation is outside frozen input"
                    )
                citation_id = stable_id(
                    "cit",
                    [
                        claim.report_claim_id,
                        pin.evidence_id,
                        pin.evidence_revision,
                        pin.source_id,
                        pin.expected_evidence_content_hash,
                    ],
                )
                if citation_id not in {item.citation_id for item in citations}:
                    citations.append(
                        pin.model_copy(update={"citation_id": citation_id})
                    )
            elif action.action is BlueActionType.REQUEST_EVIDENCE:
                acquisitions.append(
                    AcquisitionRequest(
                        acquisition_request_id=stable_id(
                            "acq",
                            [
                                verification_id,
                                claim.report_claim_id,
                                action.finding_id,
                                claim.claim_id,
                            ],
                        ),
                        report_claim_id=claim.report_claim_id,
                        finding_id=action.finding_id,
                        claim_id=claim.claim_id,
                        reason=action.reason,
                    )
                )
            if (
                action.finding_id is not None
                and action.action is not BlueActionType.REQUEST_EVIDENCE
            ):
                resolved.add(action.finding_id)
        revised = self._make_draft(
            draft.synthesis_id,
            draft.revision_number + 1,
            draft.draft_revision_id,
            draft.sections,
            tuple(sorted(claims.values(), key=lambda item: item.report_claim_id)),
            tuple(citations),
            round_number,
        )
        return revised, tuple(acquisitions), tuple(sorted(resolved))

    @staticmethod
    def _validate_judge(
        response: JudgeResponse, draft: ReportDraftRevision
    ) -> tuple[JudgeDecision, ...]:
        claims = {item.report_claim_id: item for item in draft.report_claims}
        if response.draft_revision_id != draft.draft_revision_id:
            raise VerificationContractError("Judge draft revision pin is stale")
        ids = [item.report_claim_id for item in response.decisions]
        if set(ids) != set(claims) or len(ids) != len(set(ids)):
            raise VerificationContractError(
                "Judge must decide each report claim exactly once"
            )
        for item in response.decisions:
            if (
                item.verdict is JudgeVerdict.SUPPORTED
                and claims[item.report_claim_id].structural_support
                is not StructuralSupport.STRUCTURALLY_SUPPORTED
            ):
                raise VerificationContractError(
                    "Judge SUPPORTED requires structural support"
                )
        return tuple(sorted(response.decisions, key=lambda item: item.report_claim_id))

    @staticmethod
    def _validate_continue(
        response: JudgeResponse,
        draft: ReportDraftRevision,
        findings: tuple[RedFinding, ...],
        resolved_finding_ids: tuple[str, ...],
    ) -> None:
        if response.action is not JudgeRoundAction.CONTINUE:
            return
        unresolved = {item.finding_id for item in findings} - set(resolved_finding_ids)
        has_conflict = any(
            item.structural_support is StructuralSupport.CONFLICTED
            for item in draft.report_claims
        )
        reason_valid = (
            response.continue_reason is JudgeContinueReason.OPEN_FINDINGS
            and bool(unresolved)
        ) or (
            response.continue_reason is JudgeContinueReason.STRUCTURAL_CONFLICT
            and has_conflict
        )
        if not reason_valid:
            raise VerificationContractError(
                "Judge CONTINUE reason lacks matching frozen condition"
            )


def determine_disposition(
    draft: ReportDraftRevision,
    decisions: tuple[JudgeDecision, ...],
    *,
    findings: tuple[RedFinding, ...] = (),
    resolved_finding_ids: tuple[str, ...] = (),
    citation_issues: tuple[CitationIntegrityIssue, ...] = (),
    omitted: bool,
    open_findings: bool,
    pending_acquisition: bool,
    citation_errors: bool,
) -> VerificationDisposition:
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
    publishable = [
        item
        for item in draft.report_claims
        if is_publishable(
            item.publication_state,
            verdicts[item.report_claim_id],
            has_open_blocking_finding=item.report_claim_id in blocked,
            has_citation_error=(
                global_citation_error or item.claim_id in citation_error_claims
            ),
        )
    ]
    clean = not (
        omitted or open_findings or blocked or pending_acquisition or citation_errors
    )
    if draft.report_claims and len(publishable) == len(draft.report_claims) and clean:
        return VerificationDisposition.VERIFIED
    if publishable:
        return VerificationDisposition.PARTIALLY_VERIFIED
    if (
        draft.report_claims
        and clean
        and all(
            item.verdict is JudgeVerdict.REJECTED_AS_UNSUPPORTED for item in decisions
        )
    ):
        return VerificationDisposition.REJECTED
    return VerificationDisposition.INCONCLUSIVE
