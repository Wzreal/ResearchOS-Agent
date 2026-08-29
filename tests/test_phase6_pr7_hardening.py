from __future__ import annotations

import asyncio
import json

import pytest
from conftest import FrozenClock
from test_phase5_evidence_claims import (
    NOW,
    NeverCancelled,
    browser,
    context,
    graph_services,
    observation,
)
from test_phase6_semantic_audit import LIMITS, _continued_case
from test_phase6_synthesis_verification import build_service, prepared

from researchos.adapters.claim_memory import InMemoryClaimGraphStore
from researchos.adapters.evidence_memory import InMemoryEvidenceStore
from researchos.adapters.memory import InMemoryTraceSink
from researchos.adapters.mock_verification import (
    MockVerificationModel,
    VerificationFixture,
    VerificationFixtureKey,
)
from researchos.adapters.verification_filesystem import (
    FilesystemVerificationArtifactStore,
)
from researchos.adapters.verification_memory import InMemoryVerificationArtifactStore
from researchos.application.claim_graph import ClaimGraphService
from researchos.application.errors import (
    VerificationArtifactConflict,
    VerificationContractError,
    VerificationInputCorruption,
    VerificationPersistenceError,
    VerificationUsageUncertain,
)
from researchos.application.evidence_memory import EvidenceMemory
from researchos.application.verification_coordinator import VerificationCoordinator
from researchos.application.verification_input import (
    compute_verification_id,
    derive_current_valid_edges,
    freeze_verification_input,
)
from researchos.application.verification_publisher import render_markdown
from researchos.application.verification_service import VerificationService
from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.contracts import OperatingMode
from researchos.domain.identity import stable_id
from researchos.domain.runtime import RuntimeResourceAmount
from researchos.domain.synthesis import (
    BlueAction,
    BlueActionType,
    BlueResponse,
    FindingDisposition,
    FindingType,
    JudgeDecision,
    JudgeResponse,
    JudgeVerdict,
    RedFinding,
    StructuralSupport,
    SynthesisCandidate,
    SynthesisClaim,
    VerificationDisposition,
    VerificationPolicy,
    VerificationRole,
    VerificationTerminationReason,
)


def _freeze(evidence_store, claim_store, run_id: str, policy=None):
    return freeze_verification_input(
        run_id=run_id,
        run_revision=3,
        claim_store=claim_store,
        evidence_store=evidence_store,
        policy=policy or VerificationPolicy(max_rounds=1),
    )


def test_claim_revision_makes_unchanged_support_edge_stale() -> None:
    memory, evidence_store, claim_store, graph = graph_services()
    ctx = context()
    ingested = asyncio.run(memory.ingest_observation(ctx, observation(browser())))
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_stale_claim",
        statement="revision one",
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=ingested.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    graph.revise_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        statement="revision two",
        expected_revision=1,
    )

    frozen = _freeze(evidence_store, claim_store, ctx.run_id)
    assert frozen.current_valid_edges == ()
    assert frozen.allowed_citations == ()
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    candidate = SynthesisCandidate(
        section_titles=("Section",),
        claims=(
            SynthesisClaim(
                claim_id=claim.claim.claim_id,
                claim_revision=2,
                section_ordinal=1,
                prose="revision two",
            ),
        ),
    )
    verification_id = compute_verification_id(
        frozen, VerificationPolicy(max_rounds=1), coordinator.model_bundle_hash
    )
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        candidate,
        frozen,
        VerificationPolicy(max_rounds=1),
    )
    assert draft.report_claims[0].structural_support is StructuralSupport.UNSUPPORTED
    assert draft.citations == ()


def test_evidence_revision_makes_unchanged_support_edge_stale() -> None:
    memory, evidence_store, claim_store, graph = graph_services()
    ctx = context()
    first = asyncio.run(
        memory.ingest_observation(ctx, observation(browser("revision one")))
    )
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_stale_evidence",
        statement="claim",
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=first.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    asyncio.run(
        memory.ingest_observation(
            context(attempt_id="attempt_b", attempt_number=2),
            observation(browser("revision two"), call_id="call_b"),
        )
    )
    frozen = _freeze(evidence_store, claim_store, ctx.run_id)
    assert frozen.current_valid_edges == ()
    assert frozen.allowed_citations == ()


def test_stale_support_does_not_form_conflict_candidate() -> None:
    memory, evidence_store, claim_store, graph = graph_services()
    ctx = context()
    support = asyncio.run(
        memory.ingest_observation(ctx, observation(browser("support")))
    )
    other = browser("contradiction").model_copy(
        update={"final_url": "https://example.com/other"}
    )
    contradiction = asyncio.run(
        memory.ingest_observation(
            ctx,
            observation(other, call_id="call_other", operation_key="5" * 64),
        )
    )
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_conflict",
        statement="claim",
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=support.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=contradiction.items[0].evidence_id,
        relation=ClaimEvidenceRelation.CONTRADICTS,
    )
    asyncio.run(
        memory.ingest_observation(
            context(attempt_id="attempt_b", attempt_number=2),
            observation(browser("support revised"), call_id="call_b"),
        )
    )
    frozen = _freeze(evidence_store, claim_store, ctx.run_id)
    assert frozen.conflict_candidates == ()
    assert {item.revision.relation for item in frozen.current_valid_edges} == {
        ClaimEvidenceRelation.CONTRADICTS
    }


def test_dangling_cross_store_edge_fails_closed() -> None:
    _, policy, frozen, _, _, _ = prepared()
    edge = frozen.claim_snapshot.edges[0]
    broken = frozen.claim_snapshot.model_copy(
        update={"edges": (edge.model_copy(update={"evidence_id": "evidence_missing"}),)}
    )
    with pytest.raises(VerificationInputCorruption):
        derive_current_valid_edges(broken, frozen.evidence_snapshot)


def test_blue_actions_bind_each_open_finding_and_allow_same_claim() -> None:
    _, policy, frozen, _, _, _ = prepared()
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    verification_id = compute_verification_id(
        frozen, policy, coordinator.model_bundle_hash
    )
    claim = frozen.claim_snapshot.claims[0]
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        SynthesisCandidate(
            section_titles=("Section",),
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
    report_claim_id = draft.report_claims[0].report_claim_id
    first = RedFinding(
        finding_id="find_first",
        finding_type=FindingType.OVERCLAIM,
        report_claim_id=report_claim_id,
        rationale="first",
    )
    second = first.model_copy(update={"finding_id": "find_second"})
    revised, _, resolved = coordinator._apply_blue(
        verification_id,
        draft,
        BlueResponse(
            actions=(
                BlueAction(
                    action=BlueActionType.KEEP,
                    report_claim_id=report_claim_id,
                    finding_id=first.finding_id,
                ),
                BlueAction(
                    action=BlueActionType.REQUEST_EVIDENCE,
                    report_claim_id=report_claim_id,
                    finding_id=second.finding_id,
                    reason="more evidence",
                ),
            )
        ),
        frozen,
        (first, second),
        1,
    )
    assert revised.revision_number == 2
    assert resolved == ()

    cross_claim = second.model_copy(update={"report_claim_id": "rcl_other"})
    with pytest.raises(VerificationContractError, match="differs"):
        coordinator._apply_blue(
            verification_id,
            draft,
            BlueResponse(
                actions=(
                    BlueAction(
                        action=BlueActionType.KEEP,
                        report_claim_id=report_claim_id,
                        finding_id=cross_claim.finding_id,
                    ),
                )
            ),
            frozen,
            (cross_claim,),
            1,
        )


def test_zero_citation_supported_judgement_is_rejected() -> None:
    _, policy, frozen, _, _, _ = prepared()
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    verification_id = compute_verification_id(
        frozen, policy, coordinator.model_bundle_hash
    )
    claim = frozen.claim_snapshot.claims[0]
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        SynthesisCandidate(
            section_titles=("Section",),
            claims=(
                SynthesisClaim(
                    claim_id=claim.claim_id,
                    claim_revision=claim.current_revision,
                    section_ordinal=1,
                    prose="claim",
                    evidence_ids=(),
                ),
            ),
        ),
        frozen,
        policy,
    )
    assert draft.report_claims[0].structural_support is (
        StructuralSupport.STRUCTURALLY_SUPPORTED
    )
    with pytest.raises(VerificationContractError, match="SUPPORTS citation"):
        coordinator._validate_judge(
            JudgeResponse(
                draft_revision_id=draft.draft_revision_id,
                decisions=(
                    JudgeDecision(
                        report_claim_id=draft.report_claims[0].report_claim_id,
                        verdict=JudgeVerdict.SUPPORTED,
                    ),
                ),
                finding_dispositions=(),
            ),
            draft,
            (),
            frozen,
        )


def test_qualified_conflict_requires_contradiction_assignment() -> None:
    _, policy, frozen, _, _, _ = prepared()
    support_view = frozen.current_valid_edges[0]
    conflicted = frozen.model_copy(
        update={
            "current_valid_edges": (
                support_view,
                support_view.model_copy(
                    update={
                        "edge": support_view.edge.model_copy(
                            update={
                                "edge_id": "edge_contradiction",
                                "evidence_id": "evidence_contradiction",
                            }
                        ),
                        "revision": support_view.revision.model_copy(
                            update={
                                "edge_id": "edge_contradiction",
                                "relation": ClaimEvidenceRelation.CONTRADICTS,
                            }
                        ),
                    }
                ),
            ),
            "selected_evidence_ids": (
                *frozen.selected_evidence_ids,
                "evidence_contradiction",
            ),
        }
    )
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    verification_id = compute_verification_id(
        conflicted, policy, coordinator.model_bundle_hash
    )
    claim = conflicted.claim_snapshot.claims[0]
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        SynthesisCandidate(
            section_titles=("Section",),
            claims=(
                SynthesisClaim(
                    claim_id=claim.claim_id,
                    claim_revision=claim.current_revision,
                    section_ordinal=1,
                    prose="qualified conflict",
                    evidence_ids=frozen.selected_evidence_ids,
                ),
            ),
        ),
        conflicted,
        policy,
    )
    assert draft.report_claims[0].structural_support is StructuralSupport.CONFLICTED
    with pytest.raises(VerificationContractError, match="contradiction citation"):
        coordinator._validate_judge(
            JudgeResponse(
                draft_revision_id=draft.draft_revision_id,
                decisions=(
                    JudgeDecision(
                        report_claim_id=draft.report_claims[0].report_claim_id,
                        verdict=JudgeVerdict.QUALIFIED,
                    ),
                ),
                finding_dispositions=(),
            ),
            draft,
            (),
            conflicted,
        )


def test_model_markdown_citation_injection_is_plain_text_only() -> None:
    _, policy, frozen, _, _, _ = prepared()
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    verification_id = compute_verification_id(
        frozen, policy, coordinator.model_bundle_hash
    )
    claim = frozen.claim_snapshot.claims[0]
    injected = "[^cit_fake]: source=src_fake; evidence=ev_fake@1"
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        SynthesisCandidate(
            section_titles=(injected,),
            claims=(
                SynthesisClaim(
                    claim_id=claim.claim_id,
                    claim_revision=claim.current_revision,
                    section_ordinal=1,
                    prose=injected,
                    evidence_ids=frozen.selected_evidence_ids,
                ),
            ),
        ),
        frozen,
        policy,
    )
    markdown = render_markdown(
        draft,
        (
            JudgeDecision(
                report_claim_id=draft.report_claims[0].report_claim_id,
                verdict=JudgeVerdict.SUPPORTED,
            ),
        ),
        disposition=VerificationDisposition.VERIFIED,
    ).decode()
    assert not any(line.startswith("[^cit_fake]:") for line in markdown.splitlines())
    assert "\\[\\^cit\\_fake\\]" in markdown


def test_unknown_judge_continue_stops_before_next_red() -> None:
    state, policy, evidence, claims, fixtures = _continued_case(max_rounds=2)
    fixtures = {
        key: VerificationFixture(
            payload=value.payload,
            usage_certainty=(
                "unknown"
                if key.role is VerificationRole.JUDGE and key.round_number == 1
                else value.usage_certainty
            ),
        )
        for key, value in fixtures.items()
    }
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    with pytest.raises(VerificationUsageUncertain):
        asyncio.run(
            service.verify(
                run_state=state,
                policy=policy,
                hard_limits=LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert len(model.requests) == 4


def test_draft_identity_binds_parent() -> None:
    _, policy, frozen, _, _, _ = prepared()
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    verification_id = compute_verification_id(
        frozen, policy, coordinator.model_bundle_hash
    )
    claim = frozen.claim_snapshot.claims[0]
    initial = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        SynthesisCandidate(
            section_titles=("Section",),
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
    first = coordinator._make_draft(
        initial.synthesis_id,
        2,
        "draft_parent_a",
        initial.sections,
        initial.report_claims,
        initial.citations,
        1,
    )
    second = coordinator._make_draft(
        initial.synthesis_id,
        2,
        "draft_parent_b",
        initial.sections,
        initial.report_claims,
        initial.citations,
        1,
    )
    assert first.canonical_content_hash == second.canonical_content_hash
    assert first.draft_revision_id != second.draft_revision_id


def test_same_id_different_artifact_conflicts_and_tamper_is_detected(tmp_path) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    memory_store = InMemoryVerificationArtifactStore()
    service, _ = build_service(
        evidence, claims, MockVerificationModel(fixtures), memory_store
    )
    result = asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    with pytest.raises(VerificationArtifactConflict, match="same verification"):
        memory_store.publish(
            result.model_copy(
                update={"disposition": VerificationDisposition.PARTIALLY_VERIFIED}
            ),
            memory_store.report_bytes(state.run_id) or b"",
            expected_prior_verification_id=None,
        )

    filesystem_store = FilesystemVerificationArtifactStore(tmp_path)
    filesystem_store.publish(
        result,
        memory_store.report_bytes(state.run_id) or b"",
        expected_prior_verification_id=None,
    )
    authority = tmp_path / state.run_id / "verification.json"
    payload = json.loads(authority.read_text(encoding="utf-8"))
    payload["disposition"] = VerificationDisposition.REJECTED.value
    authority.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(VerificationArtifactConflict, match="corrupt"):
        filesystem_store.load(state.run_id)


def test_authoritative_result_persists_round_and_selection_audit_data() -> None:
    state, policy, frozen, evidence, claims, fixtures = prepared()
    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    result = asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert result.claim_store_revision == frozen.claim_snapshot.store_revision
    assert result.evidence_store_revision == frozen.evidence_snapshot.store_revision
    assert result.selected_claim_ids == frozen.selected_claim_ids
    assert result.selected_evidence_ids == frozen.selected_evidence_ids
    assert result.round_records[0].input_draft_revision_id == (
        result.draft_revisions[0].draft_revision_id
    )
    assert result.termination_reason is VerificationTerminationReason.JUDGE_FINALIZED
    assert result.citation_assignments[0].relation is ClaimEvidenceRelation.SUPPORTS


def test_report_failure_exposes_committed_authority(tmp_path) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()

    def fault(stage: str) -> None:
        if stage == "report.before_temp_write":
            raise OSError("report failure")

    store = FilesystemVerificationArtifactStore(tmp_path, fault=fault)
    service, _ = build_service(evidence, claims, MockVerificationModel(fixtures), store)
    with pytest.raises(VerificationPersistenceError) as captured:
        asyncio.run(
            service.verify(
                run_state=state,
                policy=policy,
                hard_limits=LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert captured.value.authority_committed is True
    assert captured.value.artifact_committed is True
    assert captured.value.report_committed is False


def test_same_coordinator_is_reentrant_across_runs_and_modes() -> None:
    evidence_store = InMemoryEvidenceStore()
    claim_store = InMemoryClaimGraphStore()
    trace = InMemoryTraceSink()
    clock = FrozenClock(NOW)
    memory = EvidenceMemory(store=evidence_store, clock=clock, trace_sink=trace)
    graph = ClaimGraphService(
        store=claim_store,
        evidence_store=evidence_store,
        clock=clock,
        trace_sink=trace,
    )
    policy = VerificationPolicy(max_rounds=1)
    fixtures = {}
    states = []
    verification_ids = []
    base_state, _, _, _, _, _ = prepared(policy)
    helper = VerificationCoordinator(model=MockVerificationModel({}), clock=clock)
    for ordinal, (run_id, mode) in enumerate(
        (
            ("run_concurrent_mock", OperatingMode.MOCK),
            ("run_concurrent_real", OperatingMode.REAL),
        ),
        1,
    ):
        ctx = context(attempt_id=f"attempt_{ordinal}").model_copy(
            update={"run_id": run_id}
        )
        ingested = asyncio.run(
            memory.ingest_observation(
                ctx,
                observation(
                    browser(f"evidence {ordinal}"),
                    call_id=f"call_{ordinal}",
                    operation_key=str(ordinal + 5) * 64,
                ),
            )
        )
        claim = graph.create_claim(
            run_id=run_id,
            run_revision=ctx.run_revision,
            claim_scope_key=f"scope_{ordinal}",
            statement=f"claim {ordinal}",
        )
        graph.relate(
            run_id=run_id,
            run_revision=ctx.run_revision,
            claim_id=claim.claim.claim_id,
            evidence_id=ingested.items[0].evidence_id,
            relation=ClaimEvidenceRelation.SUPPORTS,
        )
        frozen = _freeze(evidence_store, claim_store, run_id, policy)
        verification_id = compute_verification_id(
            frozen, policy, helper.model_bundle_hash
        )
        verification_ids.append(verification_id)
        synthesis_id = stable_id("syn", [verification_id, "initial"])
        candidate = SynthesisCandidate(
            section_titles=(f"Section {ordinal}",),
            claims=(
                SynthesisClaim(
                    claim_id=claim.claim.claim_id,
                    claim_revision=1,
                    section_ordinal=1,
                    prose=f"claim {ordinal}",
                    evidence_ids=(ingested.items[0].evidence_id,),
                ),
            ),
        )
        initial = helper._initial_draft(synthesis_id, candidate, frozen, policy)
        revised, _, _ = helper._apply_blue(
            verification_id, initial, BlueResponse(), frozen, (), 1
        )
        fixtures.update(
            {
                VerificationFixtureKey(
                    verification_id,
                    VerificationRole.SYNTHESIZER,
                    0,
                    "draft_pending",
                ): VerificationFixture(
                    payload=candidate.model_dump(mode="json"),
                    usage=RuntimeResourceAmount(tokens=ordinal),
                ),
                VerificationFixtureKey(
                    verification_id,
                    VerificationRole.RED,
                    1,
                    initial.draft_revision_id,
                ): VerificationFixture(payload={"findings": []}),
                VerificationFixtureKey(
                    verification_id,
                    VerificationRole.BLUE,
                    1,
                    initial.draft_revision_id,
                ): VerificationFixture(payload={"actions": []}),
                VerificationFixtureKey(
                    verification_id,
                    VerificationRole.JUDGE,
                    1,
                    revised.draft_revision_id,
                ): VerificationFixture(
                    payload={
                        "draft_revision_id": revised.draft_revision_id,
                        "action": "finalize",
                        "decisions": [
                            {
                                "report_claim_id": revised.report_claims[
                                    0
                                ].report_claim_id,
                                "verdict": "supported",
                            }
                        ],
                        "finding_dispositions": [],
                    }
                ),
            }
        )
        states.append(
            base_state.model_copy(
                update={
                    "run_id": run_id,
                    "config": base_state.config.model_copy(update={"mode": mode}),
                }
            )
        )

    class ConcurrentModeModel:
        def __init__(self):
            self.delegate = MockVerificationModel(fixtures)
            self.arrived = 0
            self.both_started = asyncio.Event()

        @property
        def model_bundle_hash(self):
            return self.delegate.model_bundle_hash

        async def invoke(self, request, cancellation):
            if request.role is VerificationRole.SYNTHESIZER:
                self.arrived += 1
                if self.arrived == 2:
                    self.both_started.set()
                await self.both_started.wait()
            response = await self.delegate.invoke(request, cancellation)
            mode = "real" if request.verification_id == verification_ids[1] else "mock"
            return response.model_copy(update={"mode": mode})

    model = ConcurrentModeModel()
    coordinator = VerificationCoordinator(model=model, clock=clock)
    artifacts = InMemoryVerificationArtifactStore()
    service = VerificationService(
        claim_store=claim_store,
        evidence_store=evidence_store,
        artifact_store=artifacts,
        coordinator=coordinator,
        clock=clock,
        trace_sink=trace,
    )

    async def run_both():
        return await asyncio.gather(
            *(
                service.verify(
                    run_state=state,
                    policy=policy,
                    hard_limits=LIMITS,
                    cancellation=NeverCancelled(),
                )
                for state in states
            )
        )

    first, second = asyncio.run(run_both())
    assert first.usage.tokens == 1
    assert second.usage.tokens == 2
    assert first.verification_id != second.verification_id
    for result in (first, second):
        events = trace.read(result.run_id)
        # Evidence/Claim setup events and verification events all remain run-local.
        assert all(event.run_id == result.run_id for event in events)
        verification_events = [
            event for event in events if event.correlation_id == result.verification_id
        ]
        assert verification_events


def test_repeated_stable_finding_is_reopened_for_current_round() -> None:
    _, policy, frozen, _, _, _ = prepared(VerificationPolicy(max_rounds=2))
    support_view = frozen.current_valid_edges[0]
    contradiction_view = support_view.model_copy(
        update={
            "edge": support_view.edge.model_copy(
                update={"edge_id": "edge_contradiction"}
            ),
            "revision": support_view.revision.model_copy(
                update={
                    "edge_id": "edge_contradiction",
                    "relation": ClaimEvidenceRelation.CONTRADICTS,
                }
            ),
        }
    )
    conflicted = frozen.model_copy(
        update={"current_valid_edges": (support_view, contradiction_view)}
    )
    helper = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    verification_id = compute_verification_id(
        conflicted, policy, helper.model_bundle_hash
    )
    claim = conflicted.claim_snapshot.claims[0]
    candidate = SynthesisCandidate(
        section_titles=("Section",),
        claims=(
            SynthesisClaim(
                claim_id=claim.claim_id,
                claim_revision=claim.current_revision,
                section_ordinal=1,
                prose="claim",
                evidence_ids=conflicted.selected_evidence_ids,
            ),
        ),
    )
    initial = helper._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        candidate,
        conflicted,
        policy,
    )
    report_claim_id = initial.report_claims[0].report_claim_id
    finding_id = stable_id(
        "find",
        [verification_id, FindingType.OVERCLAIM.value, report_claim_id, None, None],
    )
    red_payload = {
        "findings": [
            {
                "finding_type": "overclaim",
                "report_claim_id": report_claim_id,
                "rationale": "same stable finding",
            }
        ]
    }
    blue_payload = BlueResponse(
        actions=(
            BlueAction(
                action=BlueActionType.KEEP,
                report_claim_id=report_claim_id,
                finding_id=finding_id,
            ),
        )
    )
    first_draft, _, _ = helper._apply_blue(
        verification_id,
        initial,
        blue_payload,
        conflicted,
        (
            RedFinding(
                finding_id=finding_id,
                finding_type=FindingType.OVERCLAIM,
                report_claim_id=report_claim_id,
                rationale="same stable finding",
            ),
        ),
        1,
    )
    second_draft, _, _ = helper._apply_blue(
        verification_id,
        first_draft,
        blue_payload,
        conflicted,
        (
            RedFinding(
                finding_id=finding_id,
                finding_type=FindingType.OVERCLAIM,
                report_claim_id=report_claim_id,
                rationale="same stable finding",
            ),
        ),
        2,
    )
    fixtures = {
        VerificationFixtureKey(
            verification_id, VerificationRole.SYNTHESIZER, 0, "draft_pending"
        ): VerificationFixture(payload=candidate.model_dump(mode="json")),
        VerificationFixtureKey(
            verification_id, VerificationRole.RED, 1, initial.draft_revision_id
        ): VerificationFixture(payload=red_payload),
        VerificationFixtureKey(
            verification_id, VerificationRole.BLUE, 1, initial.draft_revision_id
        ): VerificationFixture(payload=blue_payload.model_dump(mode="json")),
        VerificationFixtureKey(
            verification_id, VerificationRole.JUDGE, 1, first_draft.draft_revision_id
        ): VerificationFixture(
            payload={
                "draft_revision_id": first_draft.draft_revision_id,
                "action": "continue",
                "continue_reason": "structural_conflict",
                "decisions": [
                    {"report_claim_id": report_claim_id, "verdict": "unresolved"}
                ],
                "finding_dispositions": [
                    {"finding_id": finding_id, "disposition": "resolved"}
                ],
            }
        ),
        VerificationFixtureKey(
            verification_id, VerificationRole.RED, 2, first_draft.draft_revision_id
        ): VerificationFixture(payload=red_payload),
        VerificationFixtureKey(
            verification_id, VerificationRole.BLUE, 2, first_draft.draft_revision_id
        ): VerificationFixture(payload=blue_payload.model_dump(mode="json")),
        VerificationFixtureKey(
            verification_id, VerificationRole.JUDGE, 2, second_draft.draft_revision_id
        ): VerificationFixture(
            payload={
                "draft_revision_id": second_draft.draft_revision_id,
                "action": "finalize",
                "decisions": [
                    {"report_claim_id": report_claim_id, "verdict": "unresolved"}
                ],
                "finding_dispositions": [
                    {"finding_id": finding_id, "disposition": "unresolved"}
                ],
            }
        ),
    }
    coordinator = VerificationCoordinator(
        model=MockVerificationModel(fixtures), clock=FrozenClock(NOW)
    )
    result = asyncio.run(
        coordinator.execute(
            verification_id=verification_id,
            frozen=conflicted,
            policy=policy,
            hard_limits=LIMITS,
            deadline=None,
            cancellation=NeverCancelled(),
        )
    )
    resolved = result[3]
    round_records = result[9]
    assert resolved == ()
    assert round_records[0].finding_dispositions[0].disposition is (
        FindingDisposition.RESOLVED
    )
    assert round_records[1].finding_dispositions[0].disposition is (
        FindingDisposition.UNRESOLVED
    )


def test_expected_prior_absence_fails_before_model_calls() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    with pytest.raises(VerificationArtifactConflict, match="absent"):
        asyncio.run(
            service.verify(
                run_state=state,
                policy=policy,
                hard_limits=LIMITS,
                cancellation=NeverCancelled(),
                expected_prior_verification_id="ver_missing",
            )
        )
    assert model.requests == []
