"""Bounded Red/Blue/Judge coordination without scheduler or persistence ownership."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
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
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.synthesis import (
    AcquisitionRequest,
    BlueActionType,
    BlueResponse,
    FindingDisposition,
    FindingDispositionDecision,
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
    VerificationRoundRecord,
    VerificationTerminationReason,
)
from researchos.domain.verification_runtime import ModelCallStatus
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.runtime import CancellationSignal
from researchos.interfaces.verification import (
    VerificationCallJournal,
    VerificationModel,
)

T = TypeVar("T", bound=BaseModel)


@dataclass
class VerificationExecutionAccumulator:
    usage: RuntimeResourceAmount = field(default_factory=RuntimeResourceAmount)
    certainty: UsageCertainty = UsageCertainty.EXACT
    expected_mode: OperatingMode = OperatingMode.MOCK
    event_callback: Callable[[TraceEventType, dict[str, object]], None] | None = None


class VerificationCoordinator:
    def __init__(self, *, model: VerificationModel, clock: Clock) -> None:
        self._model = model
        self._clock = clock

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
        call_journal: VerificationCallJournal | None = None,
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
        tuple[VerificationRoundRecord, ...],
        VerificationTerminationReason,
    ]:
        accumulator = VerificationExecutionAccumulator(
            expected_mode=expected_mode, event_callback=event_callback
        )
        synthesis_id = stable_id("syn", [verification_id, "initial"])
        self._notify(
            accumulator,
            TraceEventType.SYNTHESIS_STARTED,
            {"synthesis_id": synthesis_id},
        )
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
            accumulator=accumulator,
            call_journal=call_journal,
        )
        draft = self._initial_draft(synthesis_id, candidate, frozen, policy)
        draft_revisions = [draft]
        self._notify(
            accumulator,
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
        open_findings: dict[str, RedFinding] = {}
        round_records: list[VerificationRoundRecord] = []
        bounds_exhausted = False
        termination_reason = VerificationTerminationReason.MAX_ROUNDS
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
                accumulator=accumulator,
                call_journal=call_journal,
            )
            current_findings = self._validate_findings(
                verification_id, red, draft, frozen
            )
            self._notify(
                accumulator,
                TraceEventType.VERIFICATION_RED_COMPLETED,
                {"round": round_number, "finding_count": len(current_findings)},
            )
            accepted: list[RedFinding] = []
            for finding in current_findings:
                if (
                    finding.finding_id in findings_by_id
                    or len(findings_by_id) < policy.max_findings
                ):
                    findings_by_id.setdefault(finding.finding_id, finding)
                    open_findings[finding.finding_id] = finding
                    resolved_finding_ids.discard(finding.finding_id)
                    accepted.append(finding)
                else:
                    bounds_exhausted = True
            if len(current_findings) != len(accepted):
                bounds_exhausted = True
            round_open = tuple(
                sorted(open_findings.values(), key=lambda item: item.finding_id)
            )
            blue = await self._call(
                BlueResponse,
                self._request(
                    verification_id,
                    VerificationRole.BLUE,
                    round_number,
                    draft,
                    frozen,
                    findings=round_open,
                ),
                policy,
                hard_limits,
                deadline,
                cancellation,
                accumulator=accumulator,
                call_journal=call_journal,
            )
            self._notify(
                accumulator,
                TraceEventType.VERIFICATION_BLUE_COMPLETED,
                {"round": round_number, "action_count": len(blue.actions)},
            )
            input_draft_id = draft.draft_revision_id
            draft, new_acquisitions, _blue_resolutions = self._apply_blue(
                verification_id,
                draft,
                blue,
                frozen,
                round_open,
                round_number,
            )
            draft_revisions.append(draft)
            self._notify(
                accumulator,
                TraceEventType.VERIFICATION_DRAFT_REVISED,
                {
                    "round": round_number,
                    "draft_revision_id": draft.draft_revision_id,
                },
            )
            for item in new_acquisitions:
                acquisitions.setdefault(item.acquisition_request_id, item)
            judge = await self._call(
                JudgeResponse,
                self._request(
                    verification_id,
                    VerificationRole.JUDGE,
                    round_number,
                    draft,
                    frozen,
                    findings=round_open,
                ),
                policy,
                hard_limits,
                deadline,
                cancellation,
                accumulator=accumulator,
                call_journal=call_journal,
            )
            decisions, dispositions = self._validate_judge(
                judge, draft, round_open, frozen
            )
            for disposition in dispositions:
                if disposition.disposition is FindingDisposition.RESOLVED:
                    resolved_finding_ids.add(disposition.finding_id)
                    open_findings.pop(disposition.finding_id, None)
                else:
                    resolved_finding_ids.discard(disposition.finding_id)
            round_records.append(
                VerificationRoundRecord(
                    round_number=round_number,
                    input_draft_revision_id=input_draft_id,
                    red_findings=tuple(accepted),
                    open_findings=round_open,
                    blue_actions=tuple(
                        sorted(
                            blue.actions,
                            key=lambda item: (
                                item.report_claim_id,
                                item.finding_id,
                                item.action.value,
                            ),
                        )
                    ),
                    output_draft_revision_id=draft.draft_revision_id,
                    judge_decisions=decisions,
                    finding_dispositions=dispositions,
                    judge_action=judge.action,
                    continue_reason=judge.continue_reason,
                )
            )
            self._notify(
                accumulator,
                TraceEventType.VERIFICATION_JUDGE_COMPLETED,
                {
                    "round": round_number,
                    "draft_revision_id": draft.draft_revision_id,
                    "decision_count": len(decisions),
                    "action": judge.action.value,
                },
            )
            self._notify(
                accumulator,
                TraceEventType.VERIFICATION_ROUND_COMPLETED,
                {"round": round_number, "draft_revision_id": draft.draft_revision_id},
            )
            if judge.action is JudgeRoundAction.FINALIZE:
                termination_reason = (
                    VerificationTerminationReason.MAX_FINDINGS
                    if bounds_exhausted
                    else VerificationTerminationReason.JUDGE_FINALIZED
                )
                break
            self._validate_continue(
                judge,
                draft,
                round_open,
                tuple(resolved_finding_ids),
            )
            if accumulator.certainty is UsageCertainty.UNKNOWN:
                raise VerificationUsageUncertain(
                    "unknown Judge usage forbids another verification model call"
                )
            if bounds_exhausted:
                termination_reason = VerificationTerminationReason.MAX_FINDINGS
                break
            if round_number == policy.max_rounds:
                bounds_exhausted = True
                termination_reason = VerificationTerminationReason.MAX_ROUNDS
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
            accumulator.usage,
            accumulator.certainty,
            bounds_exhausted,
            tuple(round_records),
            termination_reason,
        )

    async def _call(
        self,
        response_type: type[T],
        request: VerificationModelRequest,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        deadline: datetime | None,
        cancellation: CancellationSignal,
        *,
        accumulator: VerificationExecutionAccumulator | None = None,
        call_journal: VerificationCallJournal | None = None,
    ) -> T:
        accumulator = accumulator or VerificationExecutionAccumulator()
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
        prepared = None
        replay = None
        if call_journal is not None:
            response_contract_version = f"{response_type.__name__.lower()}_v1"
            prepared, replay = call_journal.prepare(
                request, response_contract_version=response_contract_version
            )
        if replay is not None:
            if replay.response_size_bytes > policy.max_model_response_bytes:
                raise VerificationContractError(
                    "committed model response exceeds byte limit"
                )
            if replay.mode != accumulator.expected_mode.value:
                raise VerificationContractError(
                    "committed verification model mode mismatch"
                )
            response_payload = response_type.model_validate(replay.validated_payload)
            self._accumulate_usage(
                accumulator,
                replay.usage,
                replay.usage_certainty,
                hard_limits,
                request.role,
            )
            return response_payload
        if prepared is not None:
            call_journal.mark_dispatched(prepared)
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
                if prepared is not None:
                    call_journal.mark_terminal(
                        prepared,
                        status=ModelCallStatus.CANCELLED,
                        failure_code="verification_cancelled",
                        usage_certainty=UsageCertainty.UNKNOWN,
                        usage=None,
                    )
                raise VerificationCancelled("verification cancelled")
            cancel_task.cancel()
            if prepared is not None:
                call_journal.mark_terminal(
                    prepared,
                    status=ModelCallStatus.TIMED_OUT,
                    failure_code="verification_deadline_exceeded",
                    usage_certainty=UsageCertainty.UNKNOWN,
                    usage=None,
                )
            raise VerificationDeadlineExceeded("verification model call timed out")
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        try:
            response = await model_task
        except VerificationCancelled:
            if prepared is not None:
                call_journal.mark_terminal(
                    prepared,
                    status=ModelCallStatus.CANCELLED,
                    failure_code="verification_cancelled",
                    usage_certainty=UsageCertainty.UNKNOWN,
                    usage=None,
                )
            raise
        except VerificationDeadlineExceeded:
            if prepared is not None:
                call_journal.mark_terminal(
                    prepared,
                    status=ModelCallStatus.TIMED_OUT,
                    failure_code="verification_deadline_exceeded",
                    usage_certainty=UsageCertainty.UNKNOWN,
                    usage=None,
                )
            raise
        except Exception as exc:
            if prepared is not None:
                call_journal.mark_terminal(
                    prepared,
                    status=ModelCallStatus.INTERRUPTED_UNKNOWN,
                    failure_code="verification_provider_outcome_unknown",
                    usage_certainty=UsageCertainty.UNKNOWN,
                    usage=None,
                )
                raise VerificationUsageUncertain(
                    "verification provider outcome is unknown"
                ) from exc
            raise VerificationModelFailure("verification provider failed") from exc
        if len(response.raw_bytes) > policy.max_model_response_bytes:
            self._mark_invalid_response(call_journal, prepared, response)
            raise VerificationContractError("model response exceeds byte limit")
        if response.mode != accumulator.expected_mode.value:
            self._mark_invalid_response(call_journal, prepared, response)
            raise VerificationContractError("verification model mode mismatch")
        if (
            response.role is not request.role
            or response.round_number != request.round_number
            or response.draft_revision_id != request.draft_revision_id
        ):
            self._mark_invalid_response(call_journal, prepared, response)
            raise VerificationContractError("verification response identity mismatch")
        try:
            # Preserve the Phase 6 ownership/order: provider usage is accounted
            # before an untrusted payload is parsed.
            self._accumulate_usage(
                accumulator,
                response.usage,
                response.usage_certainty,
                hard_limits,
                request.role,
            )
        except (VerificationContractError, VerificationUsageUncertain):
            self._mark_invalid_response(
                call_journal,
                prepared,
                response,
                failure_code="verification_usage_rejected",
            )
            raise
        try:
            payload = json.loads(response.raw_bytes)
            validated = response_type.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            self._mark_invalid_response(call_journal, prepared, response)
            raise VerificationModelFailure(
                "verification model response is malformed"
            ) from exc
        if prepared is not None:
            call_journal.commit_response(
                prepared,
                validated_payload=validated.model_dump(mode="json"),
                response=response,
            )
        return validated

    @staticmethod
    def _mark_invalid_response(
        call_journal: VerificationCallJournal | None,
        prepared,
        response,
        *,
        failure_code: str = "verification_response_invalid",
    ) -> None:
        if call_journal is not None and prepared is not None:
            call_journal.mark_terminal(
                prepared,
                status=ModelCallStatus.FAILED,
                failure_code=failure_code,
                usage_certainty=response.usage_certainty,
                usage=response.usage,
            )

    @staticmethod
    def _accumulate_usage(
        accumulator: VerificationExecutionAccumulator,
        usage: RuntimeResourceAmount,
        certainty: UsageCertainty,
        hard_limits: RuntimeResourceAmount,
        role: VerificationRole,
    ) -> None:
        accumulator.usage = accumulator.usage.plus(usage)
        if certainty is UsageCertainty.UNKNOWN:
            accumulator.certainty = UsageCertainty.UNKNOWN
        elif (
            certainty is UsageCertainty.UPPER_BOUND
            and accumulator.certainty is UsageCertainty.EXACT
        ):
            accumulator.certainty = UsageCertainty.UPPER_BOUND
        if not accumulator.usage.fits_within(hard_limits):
            raise VerificationContractError("verification hard limits exceeded")
        if certainty is UsageCertainty.UNKNOWN and role is not VerificationRole.JUDGE:
            raise VerificationUsageUncertain(
                "unknown usage stops further verification model calls"
            )

    @staticmethod
    def _notify(
        accumulator: VerificationExecutionAccumulator,
        event_type: TraceEventType,
        attributes: dict[str, object],
    ) -> None:
        if accumulator.event_callback is not None:
            accumulator.event_callback(event_type, attributes)

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
                {
                    "record": claim_records[claim_id].model_dump(mode="json"),
                    "revision": claim_revisions[
                        (claim_id, claim_records[claim_id].current_revision)
                    ].model_dump(mode="json"),
                }
                for claim_id in frozen.selected_claim_ids
            ],
            "evidence": [
                {
                    "record": evidence_records[evidence_id].model_dump(mode="json"),
                    "revision": evidence_revisions[
                        (evidence_id, evidence_records[evidence_id].current_revision)
                    ].model_dump(mode="json"),
                }
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
        relations = {
            item.revision.relation
            for item in frozen.current_valid_edges
            if item.edge.claim_id == claim_id
            and item.edge.evidence_id in frozen.selected_evidence_ids
        }
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
            draft_revision_id=stable_id(
                "draft", [synthesis_id, revision, parent, digest]
            ),
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
        findings_by_id = {item.finding_id: item for item in findings}
        allowed = {
            (item.claim_id, item.evidence_id): item for item in frozen.allowed_citations
        }
        citations = list(draft.citations)
        acquisitions: list[AcquisitionRequest] = []
        action_finding_ids = [item.finding_id for item in response.actions]
        if set(action_finding_ids) != set(findings_by_id) or len(
            action_finding_ids
        ) != len(set(action_finding_ids)):
            raise VerificationContractError(
                "Blue must act on every open finding exactly once"
            )
        by_claim: dict[str, list] = {}
        for action in response.actions:
            by_claim.setdefault(action.report_claim_id, []).append(action)
        for claim_actions in by_claim.values():
            mutations = {
                item.action
                for item in claim_actions
                if item.action
                in {
                    BlueActionType.QUALIFY,
                    BlueActionType.REMOVE,
                    BlueActionType.ADD_EXISTING_CITATION,
                }
            }
            qualified = {
                item.qualified_prose
                for item in claim_actions
                if item.action is BlueActionType.QUALIFY
            }
            if len(qualified) > 1 or (
                BlueActionType.REMOVE in mutations and len(mutations) > 1
            ):
                raise VerificationContractError("conflicting Blue mutations")
        for action in sorted(
            response.actions,
            key=lambda item: (
                item.report_claim_id,
                item.finding_id,
                item.action.value,
            ),
        ):
            claim = claims.get(action.report_claim_id)
            finding = findings_by_id.get(action.finding_id)
            if claim is None or finding is None:
                raise VerificationContractError(
                    "Blue action references unknown frozen identity"
                )
            if finding.report_claim_id != action.report_claim_id:
                raise VerificationContractError(
                    "Blue action report claim differs from its finding"
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
        revised = self._make_draft(
            draft.synthesis_id,
            draft.revision_number + 1,
            draft.draft_revision_id,
            draft.sections,
            tuple(sorted(claims.values(), key=lambda item: item.report_claim_id)),
            tuple(citations),
            round_number,
        )
        # Kept as a compatibility tuple slot; Blue never owns disposition.
        return revised, tuple(acquisitions), ()

    @staticmethod
    def _validate_judge(
        response: JudgeResponse,
        draft: ReportDraftRevision,
        findings: tuple[RedFinding, ...],
        frozen: FrozenVerificationInput,
    ) -> tuple[tuple[JudgeDecision, ...], tuple[FindingDispositionDecision, ...]]:
        claims = {item.report_claim_id: item for item in draft.report_claims}
        if response.draft_revision_id != draft.draft_revision_id:
            raise VerificationContractError("Judge draft revision pin is stale")
        ids = [item.report_claim_id for item in response.decisions]
        if set(ids) != set(claims) or len(ids) != len(set(ids)):
            raise VerificationContractError(
                "Judge must decide each report claim exactly once"
            )
        disposition_ids = [item.finding_id for item in response.finding_dispositions]
        finding_ids = {item.finding_id for item in findings}
        if set(disposition_ids) != finding_ids or len(disposition_ids) != len(
            set(disposition_ids)
        ):
            raise VerificationContractError(
                "Judge must disposition every open finding exactly once"
            )
        assignments = {
            (item.edge.claim_id, item.edge.evidence_id): item
            for item in frozen.current_valid_edges
        }
        citations_by_report: dict[str, set[ClaimEvidenceRelation]] = {}
        for citation in draft.citations:
            report_claim = next(
                item
                for item in draft.report_claims
                if item.claim_id == citation.claim_id
            )
            assignment = assignments.get((citation.claim_id, citation.evidence_id))
            if assignment is None or (
                assignment.revision.claim_revision != citation.claim_revision
                or assignment.revision.evidence_revision != citation.evidence_revision
            ):
                raise VerificationContractError(
                    "citation is not backed by a current-valid edge"
                )
            citations_by_report.setdefault(report_claim.report_claim_id, set()).add(
                assignment.revision.relation
            )
        for item in response.decisions:
            claim = claims[item.report_claim_id]
            relations = citations_by_report.get(item.report_claim_id, set())
            if (
                item.verdict is JudgeVerdict.SUPPORTED
                and claim.structural_support
                is not StructuralSupport.STRUCTURALLY_SUPPORTED
            ):
                raise VerificationContractError(
                    "Judge SUPPORTED requires structural support"
                )
            if (
                claim.publication_state is PublicationState.INCLUDED
                and item.verdict in {JudgeVerdict.SUPPORTED, JudgeVerdict.QUALIFIED}
                and ClaimEvidenceRelation.SUPPORTS not in relations
            ):
                raise VerificationContractError(
                    "publishable claim requires a current-valid SUPPORTS citation"
                )
            if (
                claim.publication_state is PublicationState.INCLUDED
                and item.verdict is JudgeVerdict.QUALIFIED
                and claim.structural_support is StructuralSupport.CONFLICTED
                and ClaimEvidenceRelation.CONTRADICTS not in relations
            ):
                raise VerificationContractError(
                    "qualified conflicted claim must disclose contradiction citation"
                )
        return (
            tuple(sorted(response.decisions, key=lambda item: item.report_claim_id)),
            tuple(
                sorted(response.finding_dispositions, key=lambda item: item.finding_id)
            ),
        )

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
