from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import FrozenClock
from test_phase5_evidence_claims import NOW, NeverCancelled
from test_phase6_synthesis_verification import prepared

from researchos.adapters import (
    verification_operation_filesystem as operation_filesystem_adapter,
)
from researchos.adapters.memory import InMemoryTraceSink
from researchos.adapters.mock_verification import (
    MockVerificationModel,
    VerificationFixture,
)
from researchos.adapters.verification_operation_filesystem import (
    FilesystemVerificationOperationStore,
)
from researchos.adapters.verification_operation_memory import (
    InMemoryVerificationOperationStore,
)
from researchos.application.errors import (
    CorruptVerificationOperation,
    RecoveryAttemptLimitExceeded,
    UnsafePersistenceData,
    VerificationContractError,
    VerificationOperationPersistenceError,
    VerificationOperationRevisionConflict,
    VerificationRecoveryInconsistency,
    VerificationUsageUncertain,
)
from researchos.application.observation_recorder import ObservationRecorder
from researchos.application.verification_coordinator import VerificationCoordinator
from researchos.application.verification_operation import (
    LEGAL_MODEL_CALL_TRANSITIONS,
    LEGAL_OPERATION_TRANSITIONS,
    VerificationOperationManager,
    validate_model_call_transition,
    validate_operation_transition,
)
from researchos.domain.contracts import (
    TraceEvent,
    TraceEventType,
    canonical_json_bytes,
    model_sha256,
)
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.synthesis import (
    VerificationModelRequest,
    VerificationModelResponse,
    VerificationRole,
)
from researchos.domain.verification_runtime import (
    MAX_OPERATION_CHECKPOINT_BYTES,
    MAX_RECOVERY_ATTEMPTS,
    ModelCallStatus,
    VerificationOperation,
    VerificationOperationEnvelope,
    VerificationOperationStatus,
    max_critical_descriptors,
    max_model_calls,
    validate_operation_capacity,
)

HARD_LIMITS = RuntimeResourceAmount(
    duration_milliseconds=10_000,
    tokens=1_000,
    cost_microunits=1_000,
    tool_calls=10,
)


def _manager(frozen, policy, *, store=None, recorder=None):
    store = store or InMemoryVerificationOperationStore()
    manager = VerificationOperationManager(
        store=store, clock=FrozenClock(NOW), recorder=recorder
    )
    model_hash = MockVerificationModel({}).model_bundle_hash
    operation = manager.create(
        frozen=frozen,
        policy=policy,
        hard_limits=HARD_LIMITS,
        model_bundle_hash=model_hash,
        verification_id=(
            __import__(
                "researchos.application.verification_input",
                fromlist=["compute_verification_id"],
            ).compute_verification_id(frozen, policy, model_hash)
        ),
    )
    return manager, store, operation


def _synthesis_request(operation, *, extra=None):
    context = {"frozen_input_hash": operation.frozen_input_hash}
    context.update(extra or {})
    return VerificationModelRequest(
        verification_id=operation.verification_id,
        role=VerificationRole.SYNTHESIZER,
        round_number=0,
        draft_revision_id="draft_pending",
        context=context,
    )


def test_capacity_formula_is_exact_at_phase6_maximum(monkeypatch) -> None:
    _, policy, _, _, _, _ = prepared()
    policy = policy.model_copy(update={"max_rounds": 10})
    assert max_model_calls(policy) == 31
    assert max_critical_descriptors(policy) == 137
    validate_operation_capacity(policy)

    monkeypatch.setattr(
        "researchos.domain.verification_runtime.RESERVED_CRITICAL_SLOTS", 136
    )
    with pytest.raises(ValueError, match="capacity"):
        validate_operation_capacity(policy)


def test_filesystem_two_writers_have_only_one_cas_winner(tmp_path) -> None:
    _, policy, frozen, _, _, _ = prepared()
    store_a = FilesystemVerificationOperationStore(tmp_path)
    manager, _, operation = _manager(frozen, policy, store=store_a)
    del manager
    store_b = FilesystemVerificationOperationStore(tmp_path)
    barrier = Barrier(2)

    def write(store):
        current = store.load(operation.run_id)
        raw = current.model_dump(mode="python")
        raw.update(
            checkpoint_revision=current.checkpoint_revision + 1,
            status=VerificationOperationStatus.EXECUTING,
            updated_at=NOW,
        )
        proposed = VerificationOperation.model_validate(raw)
        barrier.wait()
        try:
            store.save(proposed, expected_revision=current.checkpoint_revision)
            return "won"
        except VerificationOperationRevisionConflict:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, (store_a, store_b)))
    assert sorted(outcomes) == ["lost", "won"]
    assert store_a.load(operation.run_id).checkpoint_revision == 1
    assert operation_filesystem_adapter._PATH_LOCKS == {}


def test_filesystem_operation_tamper_and_post_replace_failure_are_typed(
    tmp_path,
) -> None:
    _, policy, frozen, _, _, _ = prepared()
    stages: list[str] = []

    def fault(stage: str) -> None:
        stages.append(stage)
        if stage == "after_replace":
            raise OSError("injected")

    store = FilesystemVerificationOperationStore(tmp_path, fault_injector=fault)
    with pytest.raises(VerificationOperationPersistenceError) as raised:
        _manager(frozen, policy, store=store)
    assert raised.value.checkpoint_replaced is True

    clean = FilesystemVerificationOperationStore(tmp_path)
    operation = clean.load(frozen.run_id)
    path = (
        tmp_path
        / frozen.run_id
        / "verification_operations"
        / f"{operation.verification_id}.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload_sha256"] = "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CorruptVerificationOperation):
        clean.load(operation.run_id)


def test_prepared_request_proof_rejects_changed_context_and_secret() -> None:
    _, policy, frozen, _, _, _ = prepared()
    manager, _, operation = _manager(frozen, policy)
    request = _synthesis_request(operation, extra={"safe": "one"})
    manager.session(operation.run_id).prepare(
        request, response_contract_version="synthesis_candidate_v1"
    )
    changed = _synthesis_request(operation, extra={"safe": "two"})
    with pytest.raises(VerificationRecoveryInconsistency):
        manager.session(operation.run_id).prepare(
            changed, response_contract_version="synthesis_candidate_v1"
        )

    before = manager.load(operation.run_id)
    unsafe = _synthesis_request(
        operation, extra={"authorization": "Bearer abcdefghijklmnop"}
    )
    with pytest.raises(UnsafePersistenceData):
        manager.session(operation.run_id).prepare(
            unsafe, response_contract_version="synthesis_candidate_v1"
        )
    assert manager.load(operation.run_id) == before


def test_validated_response_requiring_redaction_is_never_hashed_or_persisted() -> None:
    _, policy, frozen, _, _, _ = prepared()
    manager, _, operation = _manager(frozen, policy)
    session = manager.session(operation.run_id)
    request = _synthesis_request(operation)
    prepared_call, _ = session.prepare(
        request, response_contract_version="synthesis_candidate_v1"
    )
    session.mark_dispatched(prepared_call)
    response = VerificationModelResponse(
        raw_bytes=b"{}",
        model_id="mock_verification_v1",
        mode="mock",
        role=VerificationRole.SYNTHESIZER,
        round_number=0,
        draft_revision_id="draft_pending",
    )
    with pytest.raises(UnsafePersistenceData):
        session.commit_response(
            prepared_call,
            validated_payload={"authorization": "Bearer abcdefghijklmnop"},
            response=response,
        )
    stored = manager.load(operation.run_id).model_calls[0]
    assert stored.status is ModelCallStatus.DISPATCHED
    assert stored.response is None


def test_committed_response_restart_replays_with_zero_model_calls() -> None:
    state, policy, frozen, _, _, fixtures = prepared()
    trace = InMemoryTraceSink()
    recorder = ObservationRecorder(trace_sink=trace, clock=FrozenClock(NOW))
    manager, _, operation = _manager(
        frozen, policy, recorder=recorder
    )
    model = MockVerificationModel(fixtures)
    first = VerificationCoordinator(model=model, clock=FrozenClock(NOW))
    result = asyncio.run(
        first.execute(
            verification_id=operation.verification_id,
            frozen=frozen,
            policy=policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=1_000,
                cost_microunits=1_000,
                tool_calls=10,
            ),
            deadline=state.config.deadline,
            cancellation=NeverCancelled(),
            call_journal=manager.session(state.run_id),
        )
    )
    assert len(model.requests) == 4
    assert all(
        item.status is ModelCallStatus.RESPONSE_COMMITTED
        for item in manager.load(state.run_id).model_calls
    )

    empty_model = MockVerificationModel({})
    replayed = asyncio.run(
        VerificationCoordinator(model=empty_model, clock=FrozenClock(NOW)).execute(
            verification_id=operation.verification_id,
            frozen=frozen,
            policy=policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=1_000,
                cost_microunits=1_000,
                tool_calls=10,
            ),
            deadline=state.config.deadline,
            cancellation=NeverCancelled(),
            call_journal=manager.session(state.run_id),
        )
    )
    assert replayed == result
    assert empty_model.requests == []


def test_dispatched_without_response_becomes_unknown_and_is_not_retried() -> None:
    _, policy, frozen, _, _, _ = prepared()
    manager, _, operation = _manager(frozen, policy)
    session = manager.session(operation.run_id)
    request = _synthesis_request(operation)
    prepared_call, replay = session.prepare(
        request, response_contract_version="synthesis_candidate_v1"
    )
    assert replay is None
    session.mark_dispatched(prepared_call)

    recovered = manager.begin_recovery(operation.run_id)
    call = recovered.model_calls[0]
    assert recovered.status is VerificationOperationStatus.INTERRUPTED_UNKNOWN
    assert call.status is ModelCallStatus.INTERRUPTED_UNKNOWN
    assert call.usage_certainty is UsageCertainty.UNKNOWN
    with pytest.raises(VerificationUsageUncertain):
        session.prepare(request, response_contract_version="synthesis_candidate_v1")


def test_recovery_attempt_17_has_no_mutation_or_business_action() -> None:
    _, policy, frozen, _, _, _ = prepared()
    manager, _, operation = _manager(frozen, policy)
    for expected in range(1, MAX_RECOVERY_ATTEMPTS + 1):
        assert manager.begin_recovery(operation.run_id).recovery_attempts == expected
    before = manager.load(operation.run_id)
    with pytest.raises(RecoveryAttemptLimitExceeded) as raised:
        manager.begin_recovery(operation.run_id)
    assert raised.value.current_attempts == 16
    assert manager.load(operation.run_id) == before


def test_concurrent_recovery_attempt_16_has_one_winner_and_one_limit_error() -> None:
    _, policy, frozen, _, _, _ = prepared()
    store = InMemoryVerificationOperationStore()
    manager_a, _, operation = _manager(frozen, policy, store=store)
    manager_b = VerificationOperationManager(store=store, clock=FrozenClock(NOW))
    for _ in range(15):
        manager_a.begin_recovery(operation.run_id)
    barrier = Barrier(2)

    def recover(manager):
        barrier.wait()
        try:
            manager.begin_recovery(operation.run_id)
            return "won"
        except RecoveryAttemptLimitExceeded:
            return "limited"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(recover, (manager_a, manager_b)))
    assert sorted(outcomes) == ["limited", "won"]
    assert store.load(operation.run_id).recovery_attempts == 16


def test_operation_envelope_hash_covers_checkpoint() -> None:
    _, policy, frozen, _, _, _ = prepared()
    manager, _, operation = _manager(frozen, policy)
    assert model_sha256(manager.load(operation.run_id)) == model_sha256(operation)


def test_all_illegal_model_call_transitions_are_rejected() -> None:
    for current in ModelCallStatus:
        for target in ModelCallStatus:
            if target in LEGAL_MODEL_CALL_TRANSITIONS[current]:
                validate_model_call_transition(current, target)
            else:
                with pytest.raises(VerificationContractError, match="illegal"):
                    validate_model_call_transition(current, target)


def test_all_illegal_operation_transitions_are_rejected() -> None:
    for current in VerificationOperationStatus:
        for target in VerificationOperationStatus:
            if target in LEGAL_OPERATION_TRANSITIONS[current]:
                validate_operation_transition(current, target)
            else:
                with pytest.raises(VerificationContractError, match="illegal"):
                    validate_operation_transition(current, target)


def test_failure_before_prepared_commit_never_dispatches_model() -> None:
    class FailPrepareStore(InMemoryVerificationOperationStore):
        def save(self, operation, *, expected_revision):
            if any(
                item.status is ModelCallStatus.PREPARED
                for item in operation.model_calls
            ):
                raise RuntimeError("crash before prepared checkpoint")
            return super().save(operation, expected_revision=expected_revision)

    state, policy, frozen, _, _, fixtures = prepared()
    store = FailPrepareStore()
    manager, _, operation = _manager(frozen, policy, store=store)
    model = MockVerificationModel(fixtures)
    with pytest.raises(RuntimeError, match="prepared"):
        asyncio.run(
            VerificationCoordinator(model=model, clock=FrozenClock(NOW)).execute(
                verification_id=operation.verification_id,
                frozen=frozen,
                policy=policy,
                hard_limits=RuntimeResourceAmount(
                    duration_milliseconds=10_000,
                    tokens=1_000,
                    cost_microunits=1_000,
                    tool_calls=10,
                ),
                deadline=state.config.deadline,
                cancellation=NeverCancelled(),
                call_journal=manager.session(state.run_id),
            )
        )
    assert model.requests == []
    assert manager.load(state.run_id).model_calls == ()


def test_response_returned_but_commit_crash_recovers_as_unknown() -> None:
    class FailResponseCommitStore(InMemoryVerificationOperationStore):
        failed = False

        def save(self, operation, *, expected_revision):
            if not self.failed and any(
                item.status is ModelCallStatus.RESPONSE_COMMITTED
                for item in operation.model_calls
            ):
                self.failed = True
                raise RuntimeError("crash before response checkpoint")
            return super().save(operation, expected_revision=expected_revision)

    state, policy, frozen, _, _, fixtures = prepared()
    store = FailResponseCommitStore()
    manager, _, operation = _manager(frozen, policy, store=store)
    model = MockVerificationModel(fixtures)
    with pytest.raises(RuntimeError, match="response checkpoint"):
        asyncio.run(
            VerificationCoordinator(model=model, clock=FrozenClock(NOW)).execute(
                verification_id=operation.verification_id,
                frozen=frozen,
                policy=policy,
                hard_limits=RuntimeResourceAmount(
                    duration_milliseconds=10_000,
                    tokens=1_000,
                    cost_microunits=1_000,
                    tool_calls=10,
                ),
                deadline=state.config.deadline,
                cancellation=NeverCancelled(),
                call_journal=manager.session(state.run_id),
            )
        )
    assert len(model.requests) == 1
    assert manager.load(state.run_id).model_calls[0].status is (
        ModelCallStatus.DISPATCHED
    )
    recovered = manager.begin_recovery(state.run_id)
    assert recovered.model_calls[0].status is ModelCallStatus.INTERRUPTED_UNKNOWN
    assert recovered.model_calls[0].usage_certainty is UsageCertainty.UNKNOWN


def test_committed_response_replay_reenforces_response_byte_bound() -> None:
    state, policy, frozen, _, _, fixtures = prepared()
    manager, store, operation = _manager(frozen, policy)
    model = MockVerificationModel(fixtures)
    asyncio.run(
        VerificationCoordinator(model=model, clock=FrozenClock(NOW)).execute(
            verification_id=operation.verification_id,
            frozen=frozen,
            policy=policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=1_000,
                cost_microunits=1_000,
                tool_calls=10,
            ),
            deadline=state.config.deadline,
            cancellation=NeverCancelled(),
            call_journal=manager.session(state.run_id),
        )
    )
    current = store.load(state.run_id)
    first = current.model_calls[0]
    assert first.response is not None
    changed_response = first.response.model_copy(
        update={"response_size_bytes": policy.max_model_response_bytes + 1}
    )
    changed_call = first.model_copy(update={"response": changed_response})
    raw = current.model_dump(mode="python")
    raw.update(
        checkpoint_revision=current.checkpoint_revision + 1,
        updated_at=NOW,
        model_calls=(changed_call, *current.model_calls[1:]),
    )
    changed = VerificationOperation.model_validate(raw)
    store.save(changed, expected_revision=current.checkpoint_revision)

    empty = MockVerificationModel({})
    with pytest.raises(VerificationContractError, match="byte limit"):
        asyncio.run(
            VerificationCoordinator(model=empty, clock=FrozenClock(NOW)).execute(
                verification_id=operation.verification_id,
                frozen=frozen,
                policy=policy,
                hard_limits=RuntimeResourceAmount(
                    duration_milliseconds=10_000,
                    tokens=1_000,
                    cost_microunits=1_000,
                    tool_calls=10,
                ),
                deadline=state.config.deadline,
                cancellation=NeverCancelled(),
                call_journal=manager.session(state.run_id),
            )
        )
    assert empty.requests == []


def test_optional_outbox_degrades_deterministically_without_dropping_critical() -> None:
    _, policy, frozen, _, _, _ = prepared()
    manager, _, operation = _manager(frozen, policy)
    for index in range(98):
        manager.record_event(
            operation.run_id,
            TraceEvent(
                event_id=f"evt_optional_{index}",
                event_type=TraceEventType.VERIFICATION_ROUND_COMPLETED,
                timestamp=NOW,
                run_id=operation.run_id,
                revision=operation.run_revision,
                correlation_id=operation.verification_id,
                attributes={"round": index},
            ),
            critical=False,
        )
    bounded = manager.load(operation.run_id)
    assert sum(not item.critical for item in bounded.outbox) == 95
    assert bounded.optional_omitted_count == 3
    assert bounded.optional_omitted_hash is not None
    assert any(item.critical for item in bounded.outbox)

    final = manager.flush_optional_omission_summary(operation.run_id)
    summaries = [
        item
        for item in final.outbox
        if item.descriptor.attributes.get("omission_summary") is True
    ]
    assert len(summaries) == 1
    assert summaries[0].descriptor.event_type is (
        TraceEventType.OBSERVABILITY_OPTIONAL_OMITTED
    )
    assert summaries[0].descriptor.attributes["omitted_count"] == 3
    assert summaries[0].descriptor.attributes["omitted_hash"] == (
        final.optional_omitted_hash
    )


@pytest.mark.parametrize(
    ("case", "expected_tokens", "expected_certainty"),
    [
        ("upper", 4, UsageCertainty.UPPER_BOUND),
        ("unknown_judge", 8, UsageCertainty.UNKNOWN),
    ],
)
def test_committed_usage_replay_is_not_duplicated(
    case, expected_tokens, expected_certainty
) -> None:
    state, policy, frozen, _, _, base_fixtures = prepared()
    fixtures = {}
    for key, fixture in base_fixtures.items():
        is_unknown_judge = (
            case == "unknown_judge" and key.role is VerificationRole.JUDGE
        )
        fixtures[key] = VerificationFixture(
            payload=fixture.payload,
            usage=RuntimeResourceAmount(tokens=5 if is_unknown_judge else 1),
            usage_certainty=(
                UsageCertainty.UNKNOWN
                if is_unknown_judge
                else (
                    UsageCertainty.UPPER_BOUND
                    if case == "upper"
                    else UsageCertainty.EXACT
                )
            ),
        )
    manager, _, operation = _manager(frozen, policy)
    model = MockVerificationModel(fixtures)
    first = asyncio.run(
        VerificationCoordinator(model=model, clock=FrozenClock(NOW)).execute(
            verification_id=operation.verification_id,
            frozen=frozen,
            policy=policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=1_000,
                cost_microunits=1_000,
                tool_calls=10,
            ),
            deadline=state.config.deadline,
            cancellation=NeverCancelled(),
            call_journal=manager.session(state.run_id),
        )
    )
    replay_model = MockVerificationModel({})
    replay = asyncio.run(
        VerificationCoordinator(
            model=replay_model, clock=FrozenClock(NOW)
        ).execute(
            verification_id=operation.verification_id,
            frozen=frozen,
            policy=policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=1_000,
                cost_microunits=1_000,
                tool_calls=10,
            ),
            deadline=state.config.deadline,
            cancellation=NeverCancelled(),
            call_journal=manager.session(state.run_id),
        )
    )
    assert first[6].tokens == expected_tokens
    assert first[7] is expected_certainty
    assert replay[6].tokens == expected_tokens
    assert replay[7] is expected_certainty
    assert replay_model.requests == []


def _operation_bytes(operation: VerificationOperation) -> bytes:
    return canonical_json_bytes(
        VerificationOperationEnvelope(
            operation=operation,
            payload_sha256=model_sha256(operation),
        )
    ) + b"\n"


@pytest.mark.parametrize("kind", ["memory", "filesystem"])
def test_operation_checkpoint_just_under_limit_persists(kind, tmp_path) -> None:
    _, policy, frozen, _, _, _ = prepared()
    _, _, operation = _manager(frozen, policy)
    exact_size = len(_operation_bytes(operation))
    store = (
        InMemoryVerificationOperationStore(max_checkpoint_bytes=exact_size)
        if kind == "memory"
        else FilesystemVerificationOperationStore(
            tmp_path, max_checkpoint_bytes=exact_size
        )
    )
    store.create(operation)
    assert store.load(operation.run_id, operation.verification_id) == operation


def test_operation_checkpoint_over_limit_rejects_before_replace(tmp_path) -> None:
    _, policy, frozen, _, _, _ = prepared()
    _, _, operation = _manager(frozen, policy)
    exact_size = len(_operation_bytes(operation))
    store = FilesystemVerificationOperationStore(
        tmp_path, max_checkpoint_bytes=exact_size
    )
    store.create(operation)
    path = (
        tmp_path
        / operation.run_id
        / "verification_operations"
        / f"{operation.verification_id}.json"
    )
    before = path.read_bytes()
    oversized = operation.model_copy(
        update={
            "checkpoint_revision": 1,
            "last_error_code": "error_" + "x" * 70,
        }
    )
    with pytest.raises(CorruptVerificationOperation, match="byte limit"):
        store.save(oversized, expected_revision=0)
    assert path.read_bytes() == before


def test_tampered_oversized_operation_load_fails_typed(tmp_path) -> None:
    _, policy, frozen, _, _, _ = prepared()
    _, _, operation = _manager(frozen, policy)
    encoded = _operation_bytes(operation)
    writer = FilesystemVerificationOperationStore(
        tmp_path, max_checkpoint_bytes=len(encoded) + 1_000
    )
    writer.create(operation)
    path = (
        tmp_path
        / operation.run_id
        / "verification_operations"
        / f"{operation.verification_id}.json"
    )
    path.write_bytes(path.read_bytes() + b" " * 600)
    reader = FilesystemVerificationOperationStore(
        tmp_path, max_checkpoint_bytes=len(encoded) + 500
    )
    with pytest.raises(CorruptVerificationOperation, match="byte limit"):
        reader.load(operation.run_id, operation.verification_id)


def test_large_policy_response_bound_cannot_bypass_checkpoint_cap() -> None:
    _, policy, frozen, _, _, _ = prepared()
    policy = policy.model_copy(update={"max_model_response_bytes": 100_000_000})
    _, _, seed = _manager(frozen, policy)
    limit = len(_operation_bytes(seed)) + 8_192
    store = InMemoryVerificationOperationStore(max_checkpoint_bytes=limit)
    manager, _, operation = _manager(frozen, policy, store=store)
    request = _synthesis_request(operation)
    session = manager.session(operation.run_id, operation.verification_id)
    prepared_call, _ = session.prepare(request)
    session.mark_dispatched(prepared_call)
    response = VerificationModelResponse(
        raw_bytes=b"{}",
        usage=RuntimeResourceAmount(),
        usage_certainty=UsageCertainty.EXACT,
        model_id="mock_verification_v1",
        mode="mock",
        role=request.role,
        round_number=request.round_number,
        draft_revision_id=request.draft_revision_id,
    )
    before = store.load(operation.run_id, operation.verification_id)
    with pytest.raises(CorruptVerificationOperation, match="byte limit"):
        session.commit_response(
            prepared_call,
            validated_payload={"safe": "x" * 16_384},
            response=response,
        )
    after = store.load(operation.run_id, operation.verification_id)
    assert after == before
    with pytest.raises(ValueError, match="byte limit"):
        InMemoryVerificationOperationStore(
            max_checkpoint_bytes=MAX_OPERATION_CHECKPOINT_BYTES + 1
        )


def test_recovery_started_does_not_emit_completed_before_reconciliation() -> None:
    _, policy, frozen, _, _, _ = prepared()
    manager, _, operation = _manager(frozen, policy)
    started = manager.begin_recovery(
        operation.run_id, operation.verification_id
    )
    event_types = tuple(item.descriptor.event_type for item in started.outbox)
    assert TraceEventType.VERIFICATION_OPERATION_RECOVERY_STARTED in event_types
    assert TraceEventType.VERIFICATION_OPERATION_RECOVERY_COMPLETED not in event_types
