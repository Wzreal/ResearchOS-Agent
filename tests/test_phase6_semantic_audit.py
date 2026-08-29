from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import pytest
from conftest import FrozenClock
from pydantic import ValidationError
from test_phase5_evidence_claims import NOW, NeverCancelled
from test_phase6_synthesis_verification import (
    build_service,
    phase6_graph,
    prepared,
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
from researchos.application.claim_graph import ClaimGraphService
from researchos.application.errors import (
    VerificationArtifactConflict,
    VerificationCancelled,
    VerificationContractError,
    VerificationDeadlineExceeded,
    VerificationInputChanged,
    VerificationModelFailure,
    VerificationPersistenceError,
    VerificationPreconditionError,
    VerificationUsageUncertain,
)
from researchos.application.verification_coordinator import (
    VerificationCoordinator,
    determine_disposition,
)
from researchos.application.verification_input import (
    compute_verification_id,
    freeze_verification_input,
)
from researchos.application.verification_publisher import (
    is_publishable,
    render_markdown,
)
from researchos.application.verification_service import VerificationService
from researchos.domain.claims import (
    CitationIntegrityIssue,
    CitationIssueCode,
    CitationSeverity,
    ClaimEvidenceRelation,
)
from researchos.domain.contracts import canonical_json_bytes
from researchos.domain.identity import stable_id
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.synthesis import (
    BlueAction,
    BlueActionType,
    BlueResponse,
    FindingType,
    JudgeDecision,
    JudgeResponse,
    JudgeRoundAction,
    JudgeVerdict,
    PublicationState,
    RedCandidateFinding,
    RedResponse,
    ReportClaim,
    StructuralSupport,
    SynthesisCandidate,
    SynthesisClaim,
    VerificationDisposition,
    VerificationModelRequest,
    VerificationPolicy,
    VerificationRole,
)

LIMITS = RuntimeResourceAmount(
    duration_milliseconds=100_000,
    tokens=100_000,
    cost_microunits=100_000,
    tool_calls=100,
)


def _run(service, state, policy, *, prior=None, cancellation=None):
    return asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=LIMITS,
            cancellation=cancellation or NeverCancelled(),
            expected_prior_verification_id=prior,
        )
    )


def test_identity_formula_and_all_change_dimensions() -> None:
    _, policy, frozen, _, _, _ = prepared()
    first_model = MockVerificationModel({}, bundle_version="one")
    same = compute_verification_id(frozen, policy, first_model.model_bundle_hash)
    assert same == compute_verification_id(
        frozen, policy, first_model.model_bundle_hash
    )
    expected = stable_id(
        "ver",
        [
            frozen.run_id,
            frozen.run_revision,
            frozen.claim_snapshot_hash,
            frozen.evidence_snapshot_hash,
            hashlib.sha256(canonical_json_bytes(policy)).hexdigest(),
            first_model.model_bundle_hash,
        ],
    )
    assert same == expected
    changed_policy = policy.model_copy(update={"max_findings": policy.max_findings + 1})
    assert (
        compute_verification_id(frozen, changed_policy, first_model.model_bundle_hash)
        != same
    )
    second_model = MockVerificationModel({}, bundle_version="two")
    assert (
        compute_verification_id(frozen, policy, second_model.model_bundle_hash) != same
    )
    changed_frozen = frozen.model_copy(update={"claim_snapshot_hash": "f" * 64})
    assert (
        compute_verification_id(changed_frozen, policy, first_model.model_bundle_hash)
        != same
    )


def test_entity_identity_formulas_exclude_model_prose_and_round() -> None:
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    claim = frozen.claim_snapshot.claims[0]
    candidate = SynthesisCandidate(
        section_titles=("A",),
        claims=(
            SynthesisClaim(
                claim_id=claim.claim_id,
                claim_revision=claim.current_revision,
                section_ordinal=1,
                prose="first prose",
                evidence_ids=(frozen.selected_evidence_ids[0],),
            ),
        ),
    )
    first = coordinator._initial_draft(synthesis_id, candidate, frozen, policy)
    second = coordinator._initial_draft(
        synthesis_id,
        candidate.model_copy(
            update={
                "section_titles": ("Different title",),
                "claims": (
                    candidate.claims[0].model_copy(update={"prose": "other prose"}),
                ),
            }
        ),
        frozen,
        policy,
    )
    assert (
        first.report_claims[0].report_claim_id
        == second.report_claims[0].report_claim_id
    )
    assert first.sections[0].section_id == second.sections[0].section_id
    assert first.citations[0].citation_id == second.citations[0].citation_id
    third = coordinator._initial_draft(synthesis_id, candidate, frozen, policy)
    assert first.draft_revision_id == third.draft_revision_id
    assert first.canonical_content_hash == third.canonical_content_hash
    finding_a = coordinator._validate_findings(
        verification_id,
        RedResponse(
            findings=(
                RedCandidateFinding(
                    finding_type=FindingType.OVERCLAIM,
                    report_claim_id=first.report_claims[0].report_claim_id,
                    rationale="one",
                ),
            )
        ),
        first,
        frozen,
    )[0]
    finding_b = finding_a.model_copy(update={"rationale": "two"})
    assert finding_a.finding_id == finding_b.finding_id
    request_a = BlueResponse(
        actions=(
            BlueAction(
                action=BlueActionType.REQUEST_EVIDENCE,
                report_claim_id=first.report_claims[0].report_claim_id,
                finding_id=finding_a.finding_id,
                reason="one",
            ),
        )
    )
    _, acquisitions_a, _ = coordinator._apply_blue(
        verification_id, first, request_a, frozen, (finding_a,), 1
    )
    request_b = request_a.model_copy(
        update={"actions": (request_a.actions[0].model_copy(update={"reason": "two"}),)}
    )
    _, acquisitions_b, _ = coordinator._apply_blue(
        verification_id, first, request_b, frozen, (finding_a,), 9
    )
    assert acquisitions_a[0].acquisition_request_id == (
        acquisitions_b[0].acquisition_request_id
    )


class SpyStore:
    def __init__(self, store) -> None:
        self.store = store
        self.loads = 0
        self.fingerprints = 0

    def load(self, run_id):
        self.loads += 1
        if self.loads > 1:
            raise AssertionError("role accessed live store")
        return self.store.load(run_id)

    def snapshot_fingerprint(self, run_id):
        self.fingerprints += 1
        return self.store.snapshot_fingerprint(run_id)


def test_live_stores_load_once_and_roles_use_frozen_snapshots() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    evidence_spy = SpyStore(evidence)
    claim_spy = SpyStore(claims)
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence_spy, claim_spy, model, InMemoryVerificationArtifactStore()
    )
    _run(service, state, policy)
    assert (claim_spy.loads, evidence_spy.loads) == (1, 1)
    assert (claim_spy.fingerprints, evidence_spy.fingerprints) == (1, 1)


class MutatingModel:
    def __init__(self, delegate, evidence_store, run_id) -> None:
        self.delegate = delegate
        self.evidence_store = evidence_store
        self.run_id = run_id
        self.changed = False

    @property
    def model_bundle_hash(self):
        return self.delegate.model_bundle_hash

    async def invoke(self, request, cancellation):
        response = await self.delegate.invoke(request, cancellation)
        if not self.changed:
            current = self.evidence_store.load(self.run_id)
            self.evidence_store.save(
                current.model_copy(
                    update={"store_revision": current.store_revision + 1}
                ),
                expected_revision=current.store_revision,
            )
            self.changed = True
        return response


def test_snapshot_change_after_freeze_prevents_publication() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    artifacts = InMemoryVerificationArtifactStore()
    model = MutatingModel(MockVerificationModel(fixtures), evidence, state.run_id)
    service, _ = build_service(evidence, claims, model, artifacts)
    with pytest.raises(VerificationInputChanged, match="verification_input_changed"):
        _run(service, state, policy)
    assert artifacts.load(state.run_id) is None


def test_frozen_role_context_is_unchanged_by_later_live_mutation() -> None:
    state, policy, frozen, evidence, _, _ = prepared()
    before = VerificationCoordinator._base_context(frozen)
    current = evidence.load(state.run_id)
    evidence.save(
        current.model_copy(update={"store_revision": current.store_revision + 1}),
        expected_revision=current.store_revision,
    )
    assert VerificationCoordinator._base_context(frozen) == before


def test_whole_item_exact_and_one_byte_boundaries() -> None:
    evidence, claims, claim, ingested = phase6_graph()
    claim_snapshot = claims.load(claim.claim.run_id)
    evidence_snapshot = evidence.load(claim.claim.run_id)
    claim_size = len(canonical_json_bytes(claim_snapshot.claims[0])) + len(
        canonical_json_bytes(claim_snapshot.claim_revisions[0])
    )
    evidence_size = len(canonical_json_bytes(evidence_snapshot.evidence[0])) + len(
        canonical_json_bytes(evidence_snapshot.revisions[0])
    )
    exact_claim = freeze_verification_input(
        run_id=claim.claim.run_id,
        run_revision=1,
        claim_store=claims,
        evidence_store=evidence,
        policy=VerificationPolicy(max_model_context_bytes=claim_size),
    )
    assert exact_claim.selected_claim_ids == (claim.claim.claim_id,)
    assert exact_claim.omitted_evidence_ids == (ingested.items[0].evidence_id,)
    under = freeze_verification_input(
        run_id=claim.claim.run_id,
        run_revision=1,
        claim_store=claims,
        evidence_store=evidence,
        policy=VerificationPolicy(max_model_context_bytes=claim_size - 1),
    )
    assert under.omitted_claim_ids == (claim.claim.claim_id,)
    exact_evidence = freeze_verification_input(
        run_id=claim.claim.run_id,
        run_revision=1,
        claim_store=claims,
        evidence_store=evidence,
        policy=VerificationPolicy(max_model_context_bytes=claim_size + evidence_size),
    )
    assert exact_evidence.selected_evidence_ids == (ingested.items[0].evidence_id,)
    assert not (
        set(exact_evidence.selected_claim_ids) & set(exact_evidence.omitted_claim_ids)
    )


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "invented", "extra"],
)
def test_synthesis_requires_exactly_one_report_claim(mutation) -> None:
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    record = frozen.claim_snapshot.claims[0]
    valid = SynthesisClaim(
        claim_id=record.claim_id,
        claim_revision=record.current_revision,
        section_ordinal=1,
        prose="claim",
    )
    claims = () if mutation == "missing" else (valid,)
    if mutation == "duplicate":
        claims = (valid, valid)
    if mutation in {"invented", "extra"}:
        invented = valid.model_copy(update={"claim_id": "claim_invented"})
        claims = (invented,) if mutation == "invented" else (valid, invented)
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    with pytest.raises(VerificationContractError):
        coordinator._initial_draft(
            synthesis_id,
            SynthesisCandidate(section_titles=("S",), claims=claims),
            frozen,
            policy,
        )


@pytest.mark.parametrize(
    ("relation_set", "expected"),
    [
        (("supports",), StructuralSupport.STRUCTURALLY_SUPPORTED),
        (("supports", "contradicts"), StructuralSupport.CONFLICTED),
        ((), StructuralSupport.UNSUPPORTED),
        (("contextualizes",), StructuralSupport.UNSUPPORTED),
    ],
)
def test_runtime_structural_support_truth_table(relation_set, expected) -> None:
    _, policy, frozen, _, _, _ = prepared()
    selected = set(frozen.selected_evidence_ids)
    edge_by_id = {item.edge_id: item for item in frozen.claim_snapshot.edges}
    revisions = tuple(
        item
        for item in frozen.claim_snapshot.edge_revisions
        if item.relation.value in relation_set
    )
    if "contradicts" in relation_set and revisions:
        support = revisions[0]
        fake_edge = edge_by_id[support.edge_id].model_copy(
            update={"edge_id": "edge_contradict", "evidence_id": next(iter(selected))}
        )
        revisions = (
            support,
            support.model_copy(
                update={"edge_id": fake_edge.edge_id, "relation": "contradicts"}
            ),
        )
        snapshot = frozen.claim_snapshot.model_copy(
            update={
                "edges": (*frozen.claim_snapshot.edges, fake_edge),
                "edge_revisions": revisions,
            }
        )
    else:
        snapshot = frozen.claim_snapshot.model_copy(
            update={"edge_revisions": revisions}
        )
    candidate = frozen.model_copy(update={"claim_snapshot": snapshot})
    claim_id = frozen.selected_claim_ids[0]
    assert VerificationCoordinator._support(claim_id, candidate) is expected


def test_model_cannot_supply_structural_support_field() -> None:
    _, _, frozen, _, _, _ = prepared()
    claim = frozen.claim_snapshot.claims[0]
    with pytest.raises(ValidationError):
        SynthesisClaim.model_validate(
            {
                "claim_id": claim.claim_id,
                "claim_revision": claim.current_revision,
                "section_ordinal": 1,
                "prose": "claim",
                "structural_support": "structurally_supported",
            }
        )


def test_remove_preserves_identity_and_clears_citations() -> None:
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    claim = frozen.claim_snapshot.claims[0]
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
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
                    evidence_ids=frozen.selected_evidence_ids,
                ),
            ),
        ),
        frozen,
        policy,
    )
    revised, _, _ = coordinator._apply_blue(
        verification_id,
        draft,
        BlueResponse(
            actions=(
                BlueAction(
                    action=BlueActionType.REMOVE,
                    report_claim_id=draft.report_claims[0].report_claim_id,
                ),
            )
        ),
        frozen,
        (),
        1,
    )
    assert (
        revised.report_claims[0].report_claim_id
        == draft.report_claims[0].report_claim_id
    )
    assert revised.report_claims[0].publication_state is PublicationState.REMOVED
    assert revised.citations == ()
    assert revised.report_claims[0].report_claim_id not in (
        revised.sections[0].report_claim_ids
    )


def test_keep_round_creates_continuous_deterministic_draft_lineage() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(
        evidence, claims, MockVerificationModel(fixtures), artifacts
    )
    result = _run(service, state, policy)
    assert [item.revision_number for item in result.draft_revisions] == [1, 2]
    assert result.draft_revisions[1].parent_revision_id == (
        result.draft_revisions[0].draft_revision_id
    )
    assert (
        result.final_draft_revision_id == result.draft_revisions[-1].draft_revision_id
    )
    assert render_markdown(
        result.final_draft,
        result.judge_decisions,
        findings=result.findings,
        resolved_finding_ids=result.resolved_finding_ids,
        citation_issues=result.citation_issues,
    ) == artifacts.report_bytes(state.run_id)


@pytest.mark.parametrize(
    "finding_type",
    list(FindingType),
)
def test_all_red_finding_types_accept_valid_pins(finding_type) -> None:
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    claim = frozen.claim_snapshot.claims[0]
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
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
                    evidence_ids=frozen.selected_evidence_ids,
                ),
            ),
        ),
        frozen,
        policy,
    )
    evidence_id = (
        frozen.selected_evidence_ids[0]
        if finding_type is FindingType.CONTRADICTORY_EVIDENCE
        else None
    )
    citation_id = (
        None
        if finding_type is FindingType.CITATION_GAP
        else draft.citations[0].citation_id
    )
    result = coordinator._validate_findings(
        verification_id,
        RedResponse(
            findings=(
                RedCandidateFinding(
                    finding_type=finding_type,
                    report_claim_id=draft.report_claims[0].report_claim_id,
                    rationale="reason",
                    citation_id=citation_id,
                    evidence_id=evidence_id,
                ),
            )
        ),
        draft,
        frozen,
    )
    assert result[0].finding_type is finding_type


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_claim_id", "rcl_invented"),
        ("citation_id", "cit_invented"),
        ("evidence_id", "ev_invented"),
    ],
)
def test_red_rejects_invented_pins(field, value) -> None:
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    claim = frozen.claim_snapshot.claims[0]
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
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
                    evidence_ids=frozen.selected_evidence_ids,
                ),
            ),
        ),
        frozen,
        policy,
    )
    payload = {
        "finding_type": FindingType.OVERCLAIM,
        "report_claim_id": draft.report_claims[0].report_claim_id,
        "rationale": "reason",
        field: value,
    }
    with pytest.raises(VerificationContractError):
        coordinator._validate_findings(
            verification_id,
            RedResponse(findings=(RedCandidateFinding(**payload),)),
            draft,
            frozen,
        )


def test_red_rejects_citation_bound_to_another_report_claim() -> None:
    evidence, claims, first_claim, ingested = phase6_graph()
    graph = ClaimGraphService(
        store=claims,
        evidence_store=evidence,
        clock=FrozenClock(NOW),
        trace_sink=InMemoryTraceSink(),
    )
    second_claim = graph.create_claim(
        run_id=first_claim.claim.run_id,
        run_revision=1,
        claim_scope_key="scope_b",
        statement="Second claim",
    )
    graph.relate(
        run_id=first_claim.claim.run_id,
        run_revision=1,
        claim_id=second_claim.claim.claim_id,
        evidence_id=ingested.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    policy = VerificationPolicy(max_rounds=1)
    frozen = freeze_verification_input(
        run_id=first_claim.claim.run_id,
        run_revision=1,
        claim_store=claims,
        evidence_store=evidence,
        policy=policy,
    )
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    records = {item.claim_id: item for item in frozen.claim_snapshot.claims}
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        SynthesisCandidate(
            section_titles=("S",),
            claims=tuple(
                SynthesisClaim(
                    claim_id=claim_id,
                    claim_revision=records[claim_id].current_revision,
                    section_ordinal=1,
                    prose=f"claim {index}",
                    evidence_ids=(ingested.items[0].evidence_id,),
                )
                for index, claim_id in enumerate(frozen.selected_claim_ids)
            ),
        ),
        frozen,
        policy,
    )
    first_report = draft.report_claims[0]
    other_citation = next(
        item for item in draft.citations if item.claim_id != first_report.claim_id
    )
    with pytest.raises(VerificationContractError, match="another report claim"):
        coordinator._validate_findings(
            verification_id,
            RedResponse(
                findings=(
                    RedCandidateFinding(
                        finding_type=FindingType.OVERCLAIM,
                        report_claim_id=first_report.report_claim_id,
                        rationale="wrong citation",
                        citation_id=other_citation.citation_id,
                    ),
                )
            ),
            draft,
            frozen,
        )


def test_blue_contract_and_conflicting_actions_fail_closed() -> None:
    with pytest.raises(ValidationError):
        BlueResponse.model_validate({"actions": [], "draft": {}})
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    claim = frozen.claim_snapshot.claims[0]
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
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
    with pytest.raises(VerificationContractError, match="conflicting"):
        coordinator._apply_blue(
            verification_id,
            draft,
            BlueResponse(
                actions=(
                    BlueAction(
                        action=BlueActionType.KEEP,
                        report_claim_id=draft.report_claims[0].report_claim_id,
                    ),
                    BlueAction(
                        action=BlueActionType.REMOVE,
                        report_claim_id=draft.report_claims[0].report_claim_id,
                    ),
                )
            ),
            frozen,
            (),
            1,
        )


def test_blue_action_order_is_deterministic_across_report_claims() -> None:
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    claim = frozen.claim_snapshot.claims[0]
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
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
    second = ReportClaim(
        report_claim_id=stable_id("rcl", [synthesis_id, "claim_second", 1]),
        claim_id="claim_second",
        claim_revision=1,
        section_id=draft.sections[0].section_id,
        prose="second",
        structural_support=StructuralSupport.UNSUPPORTED,
    )
    two_claim_draft = coordinator._make_draft(
        synthesis_id,
        1,
        None,
        draft.sections,
        (*draft.report_claims, second),
        (),
        0,
    )
    actions = (
        BlueAction(
            action=BlueActionType.KEEP,
            report_claim_id=two_claim_draft.report_claims[0].report_claim_id,
        ),
        BlueAction(
            action=BlueActionType.REMOVE,
            report_claim_id=second.report_claim_id,
        ),
    )
    first, _, _ = coordinator._apply_blue(
        verification_id,
        two_claim_draft,
        BlueResponse(actions=actions),
        frozen,
        (),
        1,
    )
    second_result, _, _ = coordinator._apply_blue(
        verification_id,
        two_claim_draft,
        BlueResponse(actions=tuple(reversed(actions))),
        frozen,
        (),
        1,
    )
    assert first == second_result


def test_judge_requires_exact_current_draft_and_structural_support() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    result = _run(service, state, policy)
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    stale = JudgeResponse(
        draft_revision_id=result.draft_revisions[0].draft_revision_id,
        decisions=result.judge_decisions,
    )
    with pytest.raises(VerificationContractError, match="stale"):
        coordinator._validate_judge(stale, result.final_draft)


@pytest.mark.parametrize(
    ("omitted", "verdict", "state", "expected"),
    [
        (
            False,
            JudgeVerdict.SUPPORTED,
            PublicationState.INCLUDED,
            VerificationDisposition.VERIFIED,
        ),
        (
            True,
            JudgeVerdict.SUPPORTED,
            PublicationState.INCLUDED,
            VerificationDisposition.PARTIALLY_VERIFIED,
        ),
        (
            False,
            JudgeVerdict.REJECTED_AS_UNSUPPORTED,
            PublicationState.INCLUDED,
            VerificationDisposition.REJECTED,
        ),
        (
            False,
            JudgeVerdict.UNRESOLVED,
            PublicationState.INCLUDED,
            VerificationDisposition.INCONCLUSIVE,
        ),
    ],
)
def test_all_final_dispositions(omitted, verdict, state, expected) -> None:
    run_state, policy, _, evidence, claims, fixtures = prepared()
    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    result = _run(service, run_state, policy)
    draft = result.final_draft.model_copy(
        update={
            "report_claims": (
                result.final_draft.report_claims[0].model_copy(
                    update={"publication_state": state}
                ),
            )
        }
    )
    decision = JudgeDecision(
        report_claim_id=draft.report_claims[0].report_claim_id,
        verdict=verdict,
    )
    assert (
        determine_disposition(
            draft,
            (decision,),
            omitted=omitted,
            open_findings=verdict is JudgeVerdict.UNRESOLVED,
            pending_acquisition=False,
            citation_errors=False,
        )
        is expected
    )


def test_publishable_predicate_includes_findings_and_citation_errors() -> None:
    assert is_publishable(PublicationState.INCLUDED, JudgeVerdict.SUPPORTED)
    assert not is_publishable(PublicationState.REMOVED, JudgeVerdict.SUPPORTED)
    assert not is_publishable(
        PublicationState.INCLUDED,
        JudgeVerdict.SUPPORTED,
        has_open_blocking_finding=True,
    )
    assert not is_publishable(
        PublicationState.INCLUDED,
        JudgeVerdict.SUPPORTED,
        has_citation_error=True,
    )


def test_open_finding_prevents_rejected_and_global_citation_error_blocks_publish() -> (
    None
):
    state, policy, _, evidence, claims, fixtures = prepared()
    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    result = _run(service, state, policy)
    report_claim = result.final_draft.report_claims[0]
    finding = result.findings or (
        VerificationCoordinator._validate_findings(
            result.verification_id,
            RedResponse(
                findings=(
                    RedCandidateFinding(
                        finding_type=FindingType.OVERCLAIM,
                        report_claim_id=report_claim.report_claim_id,
                        rationale="open",
                    ),
                )
            ),
            result.final_draft,
            freeze_verification_input(
                run_id=state.run_id,
                run_revision=state.revision,
                claim_store=claims,
                evidence_store=evidence,
                policy=policy,
            ),
        )
    )
    rejected = JudgeDecision(
        report_claim_id=report_claim.report_claim_id,
        verdict=JudgeVerdict.REJECTED_AS_UNSUPPORTED,
    )
    assert (
        determine_disposition(
            result.final_draft,
            (rejected,),
            findings=finding,
            omitted=False,
            open_findings=False,
            pending_acquisition=False,
            citation_errors=False,
        )
        is VerificationDisposition.INCONCLUSIVE
    )
    global_error = CitationIntegrityIssue(
        code=CitationIssueCode.IDENTITY_MISMATCH,
        severity=CitationSeverity.ERROR,
        message="global structural error",
    )
    markdown = render_markdown(
        result.final_draft,
        result.judge_decisions,
        citation_issues=(global_error,),
    )
    assert report_claim.prose.encode() not in markdown


@pytest.mark.parametrize("role", list(VerificationRole))
def test_mock_missing_fixture_is_typed_for_every_role(role) -> None:
    model = MockVerificationModel({})
    request = VerificationModelRequest(
        verification_id="ver_a",
        role=role,
        round_number=0,
        draft_revision_id="draft_a",
        context={},
    )
    with pytest.raises(VerificationModelFailure):
        asyncio.run(model.invoke(request, NeverCancelled()))


def test_mock_wrong_response_identity_and_real_descriptor_rejected() -> None:
    with pytest.raises(VerificationModelFailure):
        MockVerificationModel({}, mode="real")  # type: ignore[arg-type]
    state, policy, _, evidence, claims, fixtures = prepared()
    key = next(key for key in fixtures if key.role is VerificationRole.SYNTHESIZER)
    fixtures[key] = VerificationFixture(
        payload=fixtures[key].payload,
        response_role=VerificationRole.RED,
    )
    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    with pytest.raises(VerificationContractError, match="identity mismatch"):
        _run(service, state, policy)


def test_all_byte_bounds_reject_before_publication() -> None:
    for bound in (
        "max_model_response_bytes",
        "max_report_bytes",
        "max_verification_artifact_bytes",
    ):
        policy = VerificationPolicy(max_rounds=1, **{bound: 10})
        state, _, _, evidence, claims, fixtures = prepared(policy)
        if bound == "max_model_response_bytes":
            key = next(
                key for key in fixtures if key.role is VerificationRole.SYNTHESIZER
            )
            fixtures[key] = VerificationFixture(payload=b"x" * 11)
        artifacts = InMemoryVerificationArtifactStore()
        service, _ = build_service(
            evidence, claims, MockVerificationModel(fixtures), artifacts
        )
        with pytest.raises(
            (
                VerificationContractError,
                VerificationModelFailure,
                VerificationPreconditionError,
            )
        ):
            _run(service, state, policy)
        assert artifacts.load(state.run_id) is None


@pytest.mark.parametrize(
    "stage",
    [
        "authority.before_temp_write",
        "authority.after_temp_write",
        "authority.after_temp_fsync",
        "authority.before_replace",
        "authority.after_replace",
        "report.before_replace",
        "report.after_replace",
    ],
)
def test_publication_fault_boundaries(stage, tmp_path) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()

    def fault(point):
        if point == stage:
            raise OSError("injected")

    store = FilesystemVerificationArtifactStore(tmp_path, fault=fault)
    model = MockVerificationModel(fixtures)
    service, _ = build_service(evidence, claims, model, store)
    with pytest.raises(VerificationPersistenceError):
        _run(service, state, policy)
    committed = store.load(state.run_id) is not None
    assert committed is (
        stage.startswith("report.") or stage == "authority.after_replace"
    )
    recovery_model = MockVerificationModel({} if committed else fixtures)
    recovery, _ = build_service(
        evidence,
        claims,
        recovery_model,
        FilesystemVerificationArtifactStore(tmp_path),
    )
    _run(recovery, state, policy)
    assert len(recovery_model.requests) == (0 if committed else 4)


class CompletionFailTrace(InMemoryTraceSink):
    def append(self, event):
        if event.event_type.value == "verification.completed":
            raise OSError("completion trace failed")
        super().append(event)


def test_completion_trace_failure_replays_without_model_call() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    artifacts = InMemoryVerificationArtifactStore()
    service = VerificationService(
        claim_store=claims,
        evidence_store=evidence,
        artifact_store=artifacts,
        coordinator=VerificationCoordinator(
            model=MockVerificationModel(fixtures), clock=FrozenClock(NOW)
        ),
        clock=FrozenClock(NOW),
        trace_sink=CompletionFailTrace(),
    )
    with pytest.raises(OSError, match="completion trace"):
        _run(service, state, policy)
    replay_model = MockVerificationModel({})
    replay, _ = build_service(evidence, claims, replay_model, artifacts)
    _run(replay, state, policy)
    assert replay_model.requests == []


def test_deterministic_markdown_and_authoritative_json(tmp_path) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    store = FilesystemVerificationArtifactStore(tmp_path)
    service, _ = build_service(evidence, claims, MockVerificationModel(fixtures), store)
    result = _run(service, state, policy)
    report = (tmp_path / state.run_id / "report.md").read_bytes()
    authority = (tmp_path / state.run_id / "verification.json").read_bytes()
    assert report.decode("utf-8").encode("utf-8") == report
    assert b"\r\n" not in report and report.endswith(b"\n")
    assert hashlib.sha256(report).hexdigest() == result.markdown_sha256
    assert authority == canonical_json_bytes(result)


def test_same_id_reconcile_and_new_verification_cas() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    artifacts = InMemoryVerificationArtifactStore()
    first_service, _ = build_service(
        evidence, claims, MockVerificationModel(fixtures), artifacts
    )
    first = _run(first_service, state, policy)
    changed_policy = policy.model_copy(update={"max_findings": policy.max_findings + 1})
    state2, _, _, evidence2, claims2, fixtures2 = prepared(changed_policy)
    wrong_model = MockVerificationModel(fixtures2)
    wrong, _ = build_service(evidence2, claims2, wrong_model, artifacts)
    with pytest.raises(VerificationArtifactConflict):
        _run(wrong, state2, changed_policy, prior="ver_wrong")
    assert wrong_model.requests == []
    correct_model = MockVerificationModel(fixtures2)
    correct, _ = build_service(evidence2, claims2, correct_model, artifacts)
    second = _run(
        correct,
        state2,
        changed_policy,
        prior=first.verification_id,
    )
    assert second.verification_id != first.verification_id
    assert second.supersedes_verification_id == first.verification_id


def test_trace_contains_no_model_or_domain_prose() -> None:
    state, policy, frozen, evidence, claims, fixtures = prepared()
    marker = "private-prose-marker"
    key = next(key for key in fixtures if key.role is VerificationRole.SYNTHESIZER)
    payload = dict(fixtures[key].payload)  # type: ignore[arg-type]
    payload["claims"][0]["prose"] = marker
    candidate = SynthesisCandidate.model_validate(payload)
    verification_id = key.verification_id
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    initial = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]), candidate, frozen, policy
    )
    revised, _, _ = coordinator._apply_blue(
        verification_id, initial, BlueResponse(), frozen, (), 1
    )
    fixtures = {
        key: VerificationFixture(payload=payload),
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
                "decisions": [
                    {
                        "report_claim_id": revised.report_claims[0].report_claim_id,
                        "verdict": "supported",
                    }
                ],
            }
        ),
    }
    service, trace = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    _run(service, state, policy)
    serialized = str(
        [item.model_dump(mode="json") for item in trace.read(state.run_id)]
    )
    assert marker not in serialized
    assert "Evidence body" not in serialized


def test_operational_cancellation_and_timeout_are_not_dispositions() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    controller = AsyncioRunCancellationController()
    controller.request_cancel()
    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    with pytest.raises(VerificationCancelled):
        _run(
            service,
            state,
            policy,
            cancellation=controller.signal_for_attempt(),
        )
    expired = state.model_copy(
        update={
            "config": state.config.model_copy(
                update={"deadline": NOW - timedelta(seconds=1)}
            )
        }
    )
    with pytest.raises(VerificationDeadlineExceeded):
        _run(service, expired, policy)


@pytest.mark.parametrize("action_type", list(BlueActionType))
def test_every_blue_action_has_bounded_effect(action_type) -> None:
    _, policy, frozen, evidence_store, claim_store, _ = prepared()
    before_evidence = evidence_store.snapshot_fingerprint(frozen.run_id)
    before_claims = claim_store.snapshot_fingerprint(frozen.run_id)
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    claim = frozen.claim_snapshot.claims[0]
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
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
    kwargs = {}
    if action_type is BlueActionType.QUALIFY:
        kwargs["qualified_prose"] = "qualified claim"
    elif action_type is BlueActionType.ADD_EXISTING_CITATION:
        kwargs["evidence_id"] = frozen.selected_evidence_ids[0]
    elif action_type is BlueActionType.REQUEST_EVIDENCE:
        kwargs["reason"] = "need more evidence"
    revised, acquisitions, _ = coordinator._apply_blue(
        verification_id,
        draft,
        BlueResponse(
            actions=(
                BlueAction(
                    action=action_type,
                    report_claim_id=draft.report_claims[0].report_claim_id,
                    **kwargs,
                ),
            )
        ),
        frozen,
        (),
        1,
    )
    assert revised.revision_number == 2
    if action_type is BlueActionType.QUALIFY:
        assert revised.report_claims[0].prose == "qualified claim"
    if action_type is BlueActionType.REMOVE:
        assert revised.report_claims[0].publication_state is PublicationState.REMOVED
    if action_type is BlueActionType.ADD_EXISTING_CITATION:
        assert len(revised.citations) == 1
    if action_type is BlueActionType.REQUEST_EVIDENCE:
        assert len(acquisitions) == 1
    assert evidence_store.snapshot_fingerprint(frozen.run_id) == before_evidence
    assert claim_store.snapshot_fingerprint(frozen.run_id) == before_claims


def test_blue_invented_citation_and_judge_content_mutation_are_rejected() -> None:
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    claim = frozen.claim_snapshot.claims[0]
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
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
    with pytest.raises(VerificationContractError):
        coordinator._apply_blue(
            verification_id,
            draft,
            BlueResponse(
                actions=(
                    BlueAction(
                        action=BlueActionType.ADD_EXISTING_CITATION,
                        report_claim_id=draft.report_claims[0].report_claim_id,
                        evidence_id="ev_invented",
                    ),
                )
            ),
            frozen,
            (),
            1,
        )
    for forbidden in ("prose", "evidence_id", "blue_action"):
        with pytest.raises(ValidationError):
            JudgeResponse.model_validate(
                {
                    "draft_revision_id": draft.draft_revision_id,
                    "decisions": [],
                    forbidden: "invented",
                }
            )


def _continued_case(*, max_rounds=2, finalize_second=True, duplicate=True):
    policy = VerificationPolicy(max_rounds=max_rounds, max_findings=2)
    state, _, frozen, evidence, claims, base = prepared(policy)
    synthesis_key = next(
        key for key in base if key.role is VerificationRole.SYNTHESIZER
    )
    candidate = SynthesisCandidate.model_validate(base[synthesis_key].payload)
    verification_id = synthesis_key.verification_id
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    initial = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]), candidate, frozen, policy
    )
    finding_payload = RedCandidateFinding(
        finding_type=FindingType.OVERCLAIM,
        report_claim_id=initial.report_claims[0].report_claim_id,
        rationale="round one",
    )
    finding = coordinator._validate_findings(
        verification_id,
        RedResponse(findings=(finding_payload,)),
        initial,
        frozen,
    )[0]
    draft_one, _, _ = coordinator._apply_blue(
        verification_id, initial, BlueResponse(), frozen, (finding,), 1
    )
    fixtures = {
        synthesis_key: base[synthesis_key],
        VerificationFixtureKey(
            verification_id, VerificationRole.RED, 1, initial.draft_revision_id
        ): VerificationFixture(
            payload={"findings": [finding_payload.model_dump(mode="json")]}
        ),
        VerificationFixtureKey(
            verification_id, VerificationRole.BLUE, 1, initial.draft_revision_id
        ): VerificationFixture(payload={"actions": []}),
        VerificationFixtureKey(
            verification_id, VerificationRole.JUDGE, 1, draft_one.draft_revision_id
        ): VerificationFixture(
            payload={
                "draft_revision_id": draft_one.draft_revision_id,
                "action": "continue",
                "continue_reason": "open_findings",
                "decisions": [
                    {
                        "report_claim_id": draft_one.report_claims[0].report_claim_id,
                        "verdict": "unresolved",
                    }
                ],
            }
        ),
    }
    if max_rounds > 1:
        round_two_payload = (
            finding_payload.model_copy(update={"rationale": "round two"})
            if duplicate
            else finding_payload.model_copy(
                update={"finding_type": FindingType.CITATION_GAP}
            )
        )
        blue_two = BlueResponse(
            actions=(
                BlueAction(
                    action=BlueActionType.QUALIFY,
                    report_claim_id=initial.report_claims[0].report_claim_id,
                    finding_id=finding.finding_id,
                    qualified_prose="qualified",
                ),
            )
        )
        draft_two, _, _ = coordinator._apply_blue(
            verification_id, draft_one, blue_two, frozen, (finding,), 2
        )
        fixtures.update(
            {
                VerificationFixtureKey(
                    verification_id,
                    VerificationRole.RED,
                    2,
                    draft_one.draft_revision_id,
                ): VerificationFixture(
                    payload={"findings": [round_two_payload.model_dump(mode="json")]}
                ),
                VerificationFixtureKey(
                    verification_id,
                    VerificationRole.BLUE,
                    2,
                    draft_one.draft_revision_id,
                ): VerificationFixture(payload=blue_two.model_dump(mode="json")),
                VerificationFixtureKey(
                    verification_id,
                    VerificationRole.JUDGE,
                    2,
                    draft_two.draft_revision_id,
                ): VerificationFixture(
                    payload={
                        "draft_revision_id": draft_two.draft_revision_id,
                        "action": "finalize" if finalize_second else "continue",
                        "continue_reason": (
                            None if finalize_second else "open_findings"
                        ),
                        "decisions": [
                            {
                                "report_claim_id": draft_two.report_claims[
                                    0
                                ].report_claim_id,
                                "verdict": "supported",
                            }
                        ],
                    }
                ),
            }
        )
    return state, policy, evidence, claims, fixtures


def test_continue_then_finalize_is_bounded_and_duplicate_findings_are_unique() -> None:
    state, policy, evidence, claims, fixtures = _continued_case()
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    result = _run(service, state, policy)
    assert len(model.requests) == 1 + 3 * 2
    assert len(result.findings) == 1
    assert result.resolved_finding_ids == (result.findings[0].finding_id,)


def test_max_round_exhaustion_is_inconclusive() -> None:
    state, policy, evidence, claims, fixtures = _continued_case(max_rounds=1)
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    result = _run(service, state, policy)
    assert len(model.requests) == 4
    assert result.disposition is VerificationDisposition.INCONCLUSIVE


def test_pending_acquisition_alone_cannot_justify_continue() -> None:
    _, policy, frozen, _, _, _ = prepared()
    model = MockVerificationModel({})
    verification_id = compute_verification_id(frozen, policy, model.model_bundle_hash)
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    claim = frozen.claim_snapshot.claims[0]
    draft = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
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
    revised, acquisitions, resolved = coordinator._apply_blue(
        verification_id,
        draft,
        BlueResponse(
            actions=(
                BlueAction(
                    action=BlueActionType.REQUEST_EVIDENCE,
                    report_claim_id=draft.report_claims[0].report_claim_id,
                    reason="need evidence",
                ),
            )
        ),
        frozen,
        (),
        1,
    )
    assert acquisitions and not resolved
    judge = JudgeResponse(
        draft_revision_id=revised.draft_revision_id,
        action=JudgeRoundAction.CONTINUE,
        continue_reason="open_findings",
        decisions=(
            JudgeDecision(
                report_claim_id=revised.report_claims[0].report_claim_id,
                verdict=JudgeVerdict.UNRESOLVED,
            ),
        ),
    )
    coordinator._validate_judge(judge, revised)
    with pytest.raises(VerificationContractError, match="CONTINUE"):
        coordinator._validate_continue(judge, revised, (), ())


def test_max_finding_exhaustion_is_bounded_and_not_verified() -> None:
    policy = VerificationPolicy(max_rounds=1, max_findings=1)
    state, _, frozen, evidence, claims, fixtures = prepared(policy)
    red_key = next(key for key in fixtures if key.role is VerificationRole.RED)
    draft_id = red_key.draft_revision_id
    synthesis_key = next(
        key for key in fixtures if key.role is VerificationRole.SYNTHESIZER
    )
    candidate = SynthesisCandidate.model_validate(fixtures[synthesis_key].payload)
    coordinator = VerificationCoordinator(
        model=MockVerificationModel({}), clock=FrozenClock(NOW)
    )
    initial = coordinator._initial_draft(
        stable_id("syn", [synthesis_key.verification_id, "initial"]),
        candidate,
        frozen,
        policy,
    )
    report_claim_id = initial.report_claims[0].report_claim_id
    fixtures[red_key] = VerificationFixture(
        payload={
            "findings": [
                {
                    "finding_type": "overclaim",
                    "report_claim_id": report_claim_id,
                    "rationale": "one",
                },
                {
                    "finding_type": "citation_gap",
                    "report_claim_id": report_claim_id,
                    "rationale": "two",
                },
            ]
        }
    )
    assert draft_id == initial.draft_revision_id
    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    result = _run(service, state, policy)
    assert len(result.findings) == 1
    assert result.disposition is VerificationDisposition.INCONCLUSIVE


@pytest.mark.parametrize(
    ("certainty", "expected"),
    [
        (UsageCertainty.EXACT, UsageCertainty.EXACT),
        (UsageCertainty.UPPER_BOUND, UsageCertainty.UPPER_BOUND),
    ],
)
def test_known_usage_aggregates_without_clamping(certainty, expected) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    fixtures = {
        key: VerificationFixture(
            payload=value.payload,
            usage=RuntimeResourceAmount(tokens=3),
            usage_certainty=certainty,
        )
        for key, value in fixtures.items()
    }
    before = state.budget
    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    result = _run(service, state, policy)
    assert result.usage.tokens == 12
    assert result.usage_certainty is expected
    assert state.budget == before


@pytest.mark.parametrize(
    "role",
    [VerificationRole.SYNTHESIZER, VerificationRole.RED, VerificationRole.BLUE],
)
def test_unknown_usage_stops_before_next_model_call(role) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    ordered = [
        VerificationRole.SYNTHESIZER,
        VerificationRole.RED,
        VerificationRole.BLUE,
        VerificationRole.JUDGE,
    ]
    fixtures = {
        key: VerificationFixture(
            payload=value.payload,
            usage=value.usage,
            usage_certainty=(
                UsageCertainty.UNKNOWN if key.role is role else UsageCertainty.EXACT
            ),
        )
        for key, value in fixtures.items()
    }
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    with pytest.raises(VerificationUsageUncertain):
        _run(service, state, policy)
    assert len(model.requests) == ordered.index(role) + 1


def test_unknown_judge_usage_is_reported_without_another_call() -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    fixtures = {
        key: VerificationFixture(
            payload=value.payload,
            usage_certainty=(
                UsageCertainty.UNKNOWN
                if key.role is VerificationRole.JUDGE
                else UsageCertainty.EXACT
            ),
        )
        for key, value in fixtures.items()
    }
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    result = _run(service, state, policy)
    assert result.usage_certainty is UsageCertainty.UNKNOWN
    assert len(model.requests) == 4


class RoleFailureModel:
    def __init__(self, delegate, role) -> None:
        self.delegate = delegate
        self.role = role
        self.calls = 0

    @property
    def model_bundle_hash(self):
        return self.delegate.model_bundle_hash

    async def invoke(self, request, cancellation):
        if request.role is self.role:
            self.calls += 1
            raise OSError("provider secret must not escape")
        return await self.delegate.invoke(request, cancellation)


@pytest.mark.parametrize("role", list(VerificationRole))
def test_provider_failure_for_each_role_is_typed_and_not_retried(role) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    delegate = MockVerificationModel(fixtures)
    model = RoleFailureModel(delegate, role)
    service, trace = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    with pytest.raises(VerificationModelFailure, match="provider failed"):
        _run(service, state, policy)
    requests_for_role = [item for item in delegate.requests if item.role is role]
    assert len(requests_for_role) == 0
    assert model.calls == 1
    assert "provider secret" not in str(
        [item.model_dump(mode="json") for item in trace.read(state.run_id)]
    )


@pytest.mark.parametrize("role", list(VerificationRole))
def test_malformed_response_for_each_role_stops_without_retry(role) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    fixtures = {
        key: (VerificationFixture(payload=b"{") if key.role is role else value)
        for key, value in fixtures.items()
    }
    model = MockVerificationModel(fixtures)
    service, _ = build_service(
        evidence, claims, model, InMemoryVerificationArtifactStore()
    )
    with pytest.raises(VerificationModelFailure):
        _run(service, state, policy)
    assert len([item for item in model.requests if item.role is role]) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("response_role", VerificationRole.RED),
        ("response_round_number", 9),
        ("response_draft_revision_id", "draft_wrong"),
    ],
)
def test_mock_response_identity_mismatch_dimensions(field, value) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    key = next(key for key in fixtures if key.role is VerificationRole.SYNTHESIZER)
    fixtures[key] = VerificationFixture(payload=fixtures[key].payload, **{field: value})
    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    with pytest.raises(VerificationContractError, match="identity mismatch"):
        _run(service, state, policy)


def test_individual_string_bounds_and_many_strings_artifact_bound() -> None:
    with pytest.raises(ValidationError):
        SynthesisClaim(
            claim_id="claim_a",
            claim_revision=1,
            section_ordinal=1,
            prose="x" * 20_001,
        )
    with pytest.raises(ValidationError):
        RedCandidateFinding(
            finding_type=FindingType.OVERCLAIM,
            report_claim_id="rcl_a",
            rationale="x" * 2_001,
        )
    with pytest.raises(ValidationError):
        SynthesisCandidate(section_titles=("x" * 201,), claims=())
    with pytest.raises(ValidationError):
        BlueAction(
            action=BlueActionType.REQUEST_EVIDENCE,
            report_claim_id="rcl_a",
            reason="x" * 2_001,
        )
    policy = VerificationPolicy(max_rounds=1, max_verification_artifact_bytes=500)
    state, _, _, evidence, claims, fixtures = prepared(policy)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(
        evidence, claims, MockVerificationModel(fixtures), artifacts
    )
    with pytest.raises(VerificationPreconditionError, match="artifact"):
        _run(service, state, policy)
    assert artifacts.load(state.run_id) is None


class NeverRoleModel:
    model_bundle_hash = MockVerificationModel({}).model_bundle_hash

    async def invoke(self, request, cancellation):
        del request, cancellation
        await asyncio.Future()


@pytest.mark.parametrize("role", list(VerificationRole))
def test_cancellation_before_dispatch_for_every_role(role) -> None:
    coordinator = VerificationCoordinator(
        model=NeverRoleModel(),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )
    controller = AsyncioRunCancellationController()
    controller.request_cancel()
    request = VerificationModelRequest(
        verification_id="ver_a",
        role=role,
        round_number=1,
        draft_revision_id="draft_a",
        context={},
    )
    with pytest.raises(VerificationCancelled):
        asyncio.run(
            coordinator._call(
                SynthesisCandidate,
                request,
                VerificationPolicy(),
                LIMITS,
                None,
                controller.signal_for_attempt(),
            )
        )


@pytest.mark.parametrize("role", list(VerificationRole))
def test_cancellation_after_dispatch_for_every_role(role) -> None:
    coordinator = VerificationCoordinator(
        model=NeverRoleModel(),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )
    controller = AsyncioRunCancellationController()
    request = VerificationModelRequest(
        verification_id="ver_a",
        role=role,
        round_number=1,
        draft_revision_id="draft_a",
        context={},
    )

    async def scenario():
        task = asyncio.create_task(
            coordinator._call(
                SynthesisCandidate,
                request,
                VerificationPolicy(),
                LIMITS,
                None,
                controller.signal_for_attempt(),
            )
        )
        await asyncio.sleep(0)
        controller.request_cancel()
        await task

    with pytest.raises(VerificationCancelled):
        asyncio.run(scenario())


@pytest.mark.parametrize("role", list(VerificationRole))
def test_timeout_bounds_never_returning_provider_for_every_role(role) -> None:
    coordinator = VerificationCoordinator(
        model=NeverRoleModel(),  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )
    request = VerificationModelRequest(
        verification_id="ver_a",
        role=role,
        round_number=1,
        draft_revision_id="draft_a",
        context={},
    )
    with pytest.raises(VerificationDeadlineExceeded):
        asyncio.run(
            coordinator._call(
                SynthesisCandidate,
                request,
                VerificationPolicy(),
                LIMITS,
                NOW + timedelta(milliseconds=5),
                NeverCancelled(),
            )
        )


def test_model_context_bound_rejects_before_dispatch() -> None:
    model = MockVerificationModel({})
    coordinator = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    request = VerificationModelRequest(
        verification_id="ver_a",
        role=VerificationRole.SYNTHESIZER,
        round_number=0,
        draft_revision_id="draft_a",
        context={"large": "x" * 100},
    )
    with pytest.raises(VerificationContractError, match="context byte"):
        asyncio.run(
            coordinator._call(
                SynthesisCandidate,
                request,
                VerificationPolicy(max_model_context_bytes=1),
                LIMITS,
                None,
                NeverCancelled(),
            )
        )
    assert model.requests == []


def test_corrupt_report_reconciles_from_unchanged_authority_without_model(
    tmp_path,
) -> None:
    state, policy, _, evidence, claims, fixtures = prepared()
    store = FilesystemVerificationArtifactStore(tmp_path)
    service, _ = build_service(evidence, claims, MockVerificationModel(fixtures), store)
    first = _run(service, state, policy)
    authority_path = tmp_path / state.run_id / "verification.json"
    report_path = tmp_path / state.run_id / "report.md"
    authority_before = authority_path.read_bytes()
    report_path.write_bytes(b"corrupt\r\n")
    replay_model = MockVerificationModel({})
    replay, _ = build_service(evidence, claims, replay_model, store)
    second = _run(replay, state, policy)
    assert second == first
    assert replay_model.requests == []
    assert authority_path.read_bytes() == authority_before
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == first.markdown_sha256
