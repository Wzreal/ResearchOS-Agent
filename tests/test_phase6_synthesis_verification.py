from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import FrozenClock
from runtime_fixtures import runtime_example
from test_phase5_evidence_claims import (
    NOW,
    NeverCancelled,
    browser,
    context,
    graph_services,
    observation,
)

from researchos.adapters.memory import InMemoryTraceSink
from researchos.adapters.mock_verification import (
    MockVerificationModel,
    VerificationFixture,
    VerificationFixtureKey,
)
from researchos.adapters.sleeper import AsyncioRunCancellationController
from researchos.adapters.verification_filesystem import (
    FilesystemVerificationArtifactStore,
)
from researchos.adapters.verification_memory import InMemoryVerificationArtifactStore
from researchos.application.errors import (
    VerificationCancelled,
    VerificationContractError,
    VerificationDeadlineExceeded,
    VerificationModelFailure,
    VerificationPersistenceError,
)
from researchos.application.verification_coordinator import VerificationCoordinator
from researchos.application.verification_input import (
    _context_bytes,
    compute_verification_id,
    freeze_verification_input,
)
from researchos.application.verification_service import VerificationService
from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.contracts import OperatingMode, RunStatus, TraceEventType
from researchos.domain.identity import stable_id
from researchos.domain.runtime import RuntimeResourceAmount
from researchos.domain.synthesis import (
    BlueAction,
    BlueActionType,
    BlueResponse,
    FindingType,
    JudgeDecision,
    JudgeResponse,
    JudgeVerdict,
    RedCandidateFinding,
    RedFinding,
    RedResponse,
    StructuralSupport,
    SynthesisCandidate,
    SynthesisClaim,
    VerificationDisposition,
    VerificationPolicy,
    VerificationRole,
)


class NeverVerificationModel:
    model_bundle_hash = MockVerificationModel({}).model_bundle_hash

    async def invoke(self, request, cancellation):
        del request, cancellation
        await asyncio.Future()


def phase6_graph():
    memory, evidence_store, claim_store, graph = graph_services()
    ctx = context()
    ingested = asyncio.run(memory.ingest_observation(ctx, observation(browser())))
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="The evidence supports this claim.",
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=ingested.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    return evidence_store, claim_store, claim, ingested


def state_verifying():
    state = runtime_example().state
    return state.model_copy(update={"status": RunStatus.VERIFYING})


def prepared(policy: VerificationPolicy | None = None):
    evidence_store, claim_store, claim, ingested = phase6_graph()
    state = state_verifying()
    policy = policy or VerificationPolicy(max_rounds=1)
    frozen = freeze_verification_input(
        run_id=state.run_id,
        run_revision=state.revision,
        claim_store=claim_store,
        evidence_store=evidence_store,
        policy=policy,
    )
    model_bundle_hash = MockVerificationModel({}).model_bundle_hash
    verification_id = compute_verification_id(frozen, policy, model_bundle_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    candidate = SynthesisCandidate(
        section_titles=("Findings",),
        claims=(
            SynthesisClaim(
                claim_id=claim.claim.claim_id,
                claim_revision=claim.claim.current_revision,
                section_ordinal=1,
                prose="The evidence supports this claim.",
                evidence_ids=(ingested.items[0].evidence_id,),
            ),
        ),
    )
    helper = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    initial = helper._initial_draft(synthesis_id, candidate, frozen, policy)
    revised, _, _ = helper._apply_blue(
        verification_id, initial, BlueResponse(), frozen, (), 1
    )
    fixtures = {
        VerificationFixtureKey(
            verification_id,
            VerificationRole.SYNTHESIZER,
            0,
            "draft_pending",
        ): VerificationFixture(payload=candidate.model_dump(mode="json")),
        VerificationFixtureKey(
            verification_id, VerificationRole.RED, 1, initial.draft_revision_id
        ): VerificationFixture(payload={"findings": []}),
        VerificationFixtureKey(
            verification_id, VerificationRole.BLUE, 1, initial.draft_revision_id
        ): VerificationFixture(payload={"actions": []}),
        VerificationFixtureKey(
            verification_id, VerificationRole.JUDGE, 1, revised.draft_revision_id
        ): VerificationFixture(
            payload={
                "draft_revision_id": revised.draft_revision_id,
                "action": "finalize",
                "finding_dispositions": [],
                "decisions": [
                    {
                        "report_claim_id": revised.report_claims[0].report_claim_id,
                        "verdict": JudgeVerdict.SUPPORTED.value,
                    }
                ],
            }
        ),
    }
    return state, policy, frozen, evidence_store, claim_store, fixtures


def build_service(evidence_store, claim_store, model, artifacts):
    clock = FrozenClock(NOW)
    trace = InMemoryTraceSink()
    return (
        VerificationService(
            claim_store=claim_store,
            evidence_store=evidence_store,
            artifact_store=artifacts,
            coordinator=VerificationCoordinator(model=model, clock=clock),
            clock=clock,
            trace_sink=trace,
        ),
        trace,
    )


def test_end_to_end_verified_and_same_identity_replay_has_no_model_calls() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, trace = build_service(evidence_store, claim_store, model, artifacts)
    result = asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=1_000,
                cost_microunits=1_000,
                tool_calls=10,
            ),
            cancellation=NeverCancelled(),
        )
    )
    assert result.disposition is VerificationDisposition.VERIFIED
    assert result.final_draft.report_claims[0].structural_support is (
        StructuralSupport.STRUCTURALLY_SUPPORTED
    )
    assert artifacts.report_bytes(state.run_id).endswith(b"\n")
    assert len(model.requests) == 4
    replay = asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=RuntimeResourceAmount(),
            cancellation=NeverCancelled(),
        )
    )
    assert replay == result
    assert len(model.requests) == 4
    assert trace.read(state.run_id)[-1].event_type is (
        TraceEventType.VERIFICATION_PUBLICATION_REPLAYED
    )


def test_stable_finding_identity_excludes_rationale_and_round() -> None:
    _, policy, frozen, _, _, _ = prepared()
    verification_id = compute_verification_id(
        frozen, policy, MockVerificationModel({}).model_bundle_hash
    )
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    claim = frozen.claim_snapshot.claims[0]
    candidate = SynthesisCandidate(
        section_titles=("S",),
        claims=(
            SynthesisClaim(
                claim_id=claim.claim_id,
                claim_revision=claim.current_revision,
                section_ordinal=1,
                prose="p",
            ),
        ),
    )
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    draft = coordinator._initial_draft(synthesis_id, candidate, frozen, policy)
    first = coordinator._validate_findings(
        verification_id,
        RedResponse(
            findings=(
                RedCandidateFinding(
                    finding_type=FindingType.OVERCLAIM,
                    report_claim_id=draft.report_claims[0].report_claim_id,
                    rationale="first rationale",
                ),
            )
        ),
        draft,
        frozen,
    )[0]
    second = first.model_copy(update={"rationale": "other model prose"})
    assert first.finding_id == second.finding_id


def test_red_finding_pin_invariants() -> None:
    with pytest.raises(ValueError):
        RedFinding(
            finding_id="find_a",
            finding_type=FindingType.CONTRADICTORY_EVIDENCE,
            report_claim_id="rcl_a",
            rationale="reason",
        )
    with pytest.raises(ValueError):
        RedFinding(
            finding_id="find_a",
            finding_type=FindingType.CITATION_GAP,
            report_claim_id="rcl_a",
            rationale="reason",
            citation_id="cit_a",
        )


def test_whole_item_admission_omits_claim_without_truncating() -> None:
    evidence_store, claim_store, claim, ingested = phase6_graph()
    omitted_size = _context_bytes(
        claims=[],
        evidence=[],
        citations=[],
        conflicts=[],
        omitted_claim_ids=[claim.claim.claim_id],
        omitted_evidence_ids=[ingested.items[0].evidence_id],
    )
    policy = VerificationPolicy(max_rounds=1, max_model_context_bytes=omitted_size)
    frozen = freeze_verification_input(
        run_id=context().run_id,
        run_revision=context().run_revision,
        claim_store=claim_store,
        evidence_store=evidence_store,
        policy=policy,
    )
    assert frozen.selected_claim_ids == ()
    assert frozen.omitted_claim_ids == (claim.claim.claim_id,)


def test_deadline_before_model_call_is_explicit() -> None:
    state, policy, frozen, _, _, _ = prepared()
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    with pytest.raises(VerificationDeadlineExceeded):
        asyncio.run(
            coordinator.execute(
                verification_id=compute_verification_id(
                    frozen, policy, coordinator.model_bundle_hash
                ),
                frozen=frozen,
                policy=policy,
                hard_limits=RuntimeResourceAmount(),
                deadline=NOW - timedelta(seconds=1),
                cancellation=NeverCancelled(),
            )
        )
    assert state.status is RunStatus.VERIFYING


def test_cancellation_before_model_call_is_explicit() -> None:
    _, policy, frozen, _, _, _ = prepared()
    controller = AsyncioRunCancellationController()
    controller.request_cancel()
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    with pytest.raises(VerificationCancelled):
        asyncio.run(
            coordinator.execute(
                verification_id=compute_verification_id(
                    frozen, policy, coordinator.model_bundle_hash
                ),
                frozen=frozen,
                policy=policy,
                hard_limits=RuntimeResourceAmount(),
                deadline=None,
                cancellation=controller.signal_for_attempt(),
            )
        )


def test_cancellation_during_never_returning_model_call() -> None:
    _, policy, frozen, _, _, _ = prepared()
    controller = AsyncioRunCancellationController()
    coordinator = VerificationCoordinator(
        model=NeverVerificationModel(),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )

    async def scenario():
        operation = asyncio.create_task(
            coordinator.execute(
                verification_id=compute_verification_id(
                    frozen, policy, coordinator.model_bundle_hash
                ),
                frozen=frozen,
                policy=policy,
                hard_limits=RuntimeResourceAmount(),
                deadline=None,
                cancellation=controller.signal_for_attempt(),
            )
        )
        await asyncio.sleep(0)
        controller.request_cancel()
        await operation

    with pytest.raises(VerificationCancelled):
        asyncio.run(scenario())


def test_deadline_bounds_never_returning_model_call() -> None:
    _, policy, frozen, _, _, _ = prepared()
    coordinator = VerificationCoordinator(
        model=NeverVerificationModel(),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )
    with pytest.raises(VerificationDeadlineExceeded):
        asyncio.run(
            coordinator.execute(
                verification_id=compute_verification_id(
                    frozen, policy, coordinator.model_bundle_hash
                ),
                frozen=frozen,
                policy=policy,
                hard_limits=RuntimeResourceAmount(),
                deadline=NOW + timedelta(milliseconds=10),
                cancellation=NeverCancelled(),
            )
        )


def test_filesystem_authority_survives_report_write_failure(tmp_path) -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()

    def fault(stage: str) -> None:
        if stage == "report.before_temp_write":
            raise OSError("injected")

    store = FilesystemVerificationArtifactStore(tmp_path, fault=fault)
    service, _ = build_service(
        evidence_store, claim_store, MockVerificationModel(fixtures), store
    )
    with pytest.raises(VerificationPersistenceError):
        asyncio.run(
            service.verify(
                run_state=state,
                policy=policy,
                hard_limits=RuntimeResourceAmount(
                    duration_milliseconds=10_000,
                    tokens=1_000,
                    cost_microunits=1_000,
                    tool_calls=10,
                ),
                cancellation=NeverCancelled(),
            )
        )
    assert store.load(state.run_id) is not None
    recovered_store = FilesystemVerificationArtifactStore(tmp_path)
    recovered_model = MockVerificationModel({})
    recovered, _ = build_service(
        evidence_store, claim_store, recovered_model, recovered_store
    )
    result = asyncio.run(
        recovered.verify(
            run_state=state,
            policy=policy,
            hard_limits=RuntimeResourceAmount(),
            cancellation=NeverCancelled(),
        )
    )
    assert result.run_id == state.run_id
    assert recovered_model.requests == []
    assert (tmp_path / state.run_id / "report.md").exists()


def test_model_cannot_mark_unsupported_claim_supported() -> None:
    _, policy, frozen, _, _, _ = prepared()
    claim = frozen.claim_snapshot.claims[0]
    unsupported = frozen.model_copy(update={"selected_evidence_ids": ()})
    verification_id = compute_verification_id(
        unsupported, policy, MockVerificationModel({}).model_bundle_hash
    )
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    draft = coordinator._initial_draft(
        synthesis_id,
        SynthesisCandidate(
            section_titles=("S",),
            claims=(
                SynthesisClaim(
                    claim_id=claim.claim_id,
                    claim_revision=claim.current_revision,
                    section_ordinal=1,
                    prose="unsupported",
                ),
            ),
        ),
        unsupported,
        policy,
    )
    assert draft.report_claims[0].structural_support is StructuralSupport.UNSUPPORTED
    with pytest.raises(VerificationContractError):
        coordinator._validate_judge(
            response=JudgeResponse(
                draft_revision_id=draft.draft_revision_id,
                decisions=(
                    JudgeDecision(
                        report_claim_id=draft.report_claims[0].report_claim_id,
                        verdict=JudgeVerdict.SUPPORTED,
                    ),
                ),
                finding_dispositions=(),
            ),
            draft=draft,
            findings=(),
            frozen=frozen,
        )


def test_remove_preserves_report_claim_entity() -> None:
    _, policy, frozen, _, _, _ = prepared()
    verification_id = compute_verification_id(
        frozen, policy, MockVerificationModel({}).model_bundle_hash
    )
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    claim = frozen.claim_snapshot.claims[0]
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    draft = coordinator._initial_draft(
        synthesis_id,
        SynthesisCandidate(
            section_titles=("S",),
            claims=(
                SynthesisClaim(
                    claim_id=claim.claim_id,
                    claim_revision=claim.current_revision,
                    section_ordinal=1,
                    prose="claim",
                ),
            ),
        ),
        frozen,
        policy,
    )
    finding = RedFinding(
        finding_id="find_remove",
        finding_type=FindingType.OVERCLAIM,
        report_claim_id=draft.report_claims[0].report_claim_id,
        rationale="remove finding",
    )
    revised, _, _ = coordinator._apply_blue(
        verification_id,
        draft,
        BlueResponse(
            actions=(
                BlueAction(
                    action=BlueActionType.REMOVE,
                    report_claim_id=draft.report_claims[0].report_claim_id,
                    finding_id=finding.finding_id,
                ),
            )
        ),
        frozen,
        (finding,),
        1,
    )
    assert revised.report_claims[0].report_claim_id == (
        draft.report_claims[0].report_claim_id
    )
    assert revised.report_claims[0].publication_state.value == "removed"


def test_malformed_model_response_is_typed_and_sanitized() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    first_key = next(
        key for key in fixtures if key.role is VerificationRole.SYNTHESIZER
    )
    fixtures[first_key] = VerificationFixture(
        payload=b'{"secret":"Bearer abcdefghijklmnop"'
    )
    service, trace = build_service(
        evidence_store,
        claim_store,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    with pytest.raises(VerificationModelFailure):
        asyncio.run(
            service.verify(
                run_state=state,
                policy=policy,
                hard_limits=RuntimeResourceAmount(),
                cancellation=NeverCancelled(),
            )
        )
    encoded_trace = str([item.model_dump() for item in trace.read(state.run_id)])
    assert "abcdefghijklmnop" not in encoded_trace


def test_mock_model_is_rejected_for_real_mode() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    real_state = state.model_copy(
        update={"config": state.config.model_copy(update={"mode": OperatingMode.REAL})}
    )
    service, _ = build_service(
        evidence_store,
        claim_store,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    with pytest.raises(VerificationContractError, match="mode mismatch"):
        asyncio.run(
            service.verify(
                run_state=real_state,
                policy=policy,
                hard_limits=RuntimeResourceAmount(),
                cancellation=NeverCancelled(),
            )
        )


def test_usage_hard_limit_stops_after_first_model_call() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    first_key = next(
        key for key in fixtures if key.role is VerificationRole.SYNTHESIZER
    )
    original = fixtures[first_key]
    fixtures[first_key] = VerificationFixture(
        payload=original.payload,
        usage=RuntimeResourceAmount(tokens=2),
    )
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence_store,
        claim_store,
        model,
        InMemoryVerificationArtifactStore(),
    )
    with pytest.raises(VerificationContractError, match="hard limits"):
        asyncio.run(
            service.verify(
                run_state=state,
                policy=policy,
                hard_limits=RuntimeResourceAmount(tokens=1),
                cancellation=NeverCancelled(),
            )
        )
    assert len(model.requests) == 1
