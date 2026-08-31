from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from conftest import FrozenClock, SequentialIds
from pydantic import ValidationError
from runtime_fixtures import build_executor, runtime_example
from test_phase5_evidence_claims import NOW, NeverCancelled
from test_phase6_synthesis_verification import (
    NeverVerificationModel,
    build_service,
    prepared,
)

from researchos.adapters.memory import InMemoryRunStore, InMemoryTraceSink
from researchos.adapters.mock_execution import (
    ExecutionFixture,
    ExecutionFixtureKey,
    MockTaskExecutionBackend,
    successful_result,
)
from researchos.adapters.mock_observability import MockObservationExporter
from researchos.adapters.mock_verification import (
    MockVerificationModel,
    VerificationFixture,
    VerificationFixtureKey,
)
from researchos.adapters.sleeper import AsyncioRunCancellationController
from researchos.adapters.verification_memory import InMemoryVerificationArtifactStore
from researchos.adapters.verification_operation_memory import (
    InMemoryVerificationOperationStore,
)
from researchos.application.claim_graph import ClaimGraphService
from researchos.application.durable_verification import (
    DurableVerificationCoordinator,
)
from researchos.application.errors import (
    VerificationCancelled,
    VerificationContractError,
    VerificationDeadlineExceeded,
    VerificationModelFailure,
    VerificationOperationRevisionConflict,
    VerificationPreconditionError,
    VerificationRecoveryInconsistency,
    VerificationUsageUncertain,
)
from researchos.application.observation_recorder import (
    ExporterDispatcher,
    ObservationRecorder,
)
from researchos.application.run_manager import RunManager
from researchos.application.verification_coordinator import VerificationCoordinator
from researchos.application.verification_input import (
    compute_verification_id,
    freeze_verification_input,
)
from researchos.application.verification_lineage import (
    reconstruct_verification_lineage,
)
from researchos.application.verification_operation import VerificationOperationManager
from researchos.application.verification_service import VerificationService
from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.contracts import (
    RunConfig,
    RunInput,
    RunState,
    RunStatus,
    TraceEventType,
    model_sha256,
)
from researchos.domain.identity import stable_id
from researchos.domain.observability import ExporterPolicy
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.synthesis import (
    BlueResponse,
    JudgeVerdict,
    SynthesisCandidate,
    VerificationModelRequest,
    VerificationRole,
)
from researchos.domain.verification_runtime import (
    ModelCallStatus,
    VerificationOperationStatus,
)

HARD_LIMITS = RuntimeResourceAmount(
    duration_milliseconds=10_000,
    tokens=1_000,
    cost_microunits=1_000,
    tool_calls=10,
)


class StubRunManager:
    def __init__(self, state: RunState) -> None:
        self.state = state
        self.transitions: list[RunStatus] = []

    def load(self, run_id: str) -> RunState:
        assert run_id == self.state.run_id
        return self.state.model_copy(deep=True)

    def transition(self, run_id: str, status: RunStatus) -> RunState:
        assert run_id == self.state.run_id
        self.transitions.append(status)
        raw = self.state.model_dump(mode="python")
        raw.update(
            revision=self.state.revision + 1,
            status=status,
            updated_at=NOW,
            last_transition_id=f"tr_phase8_{len(self.transitions)}",
        )
        self.state = RunState.model_validate(raw)
        return self.state.model_copy(deep=True)


def _durable(
    state,
    service,
    artifacts,
    *,
    trace=None,
    operation_store=None,
    checkpoint_manager=None,
):
    trace = trace or InMemoryTraceSink()
    recorder = ObservationRecorder(trace_sink=trace, clock=FrozenClock(NOW))
    operations = VerificationOperationManager(
        store=operation_store or InMemoryVerificationOperationStore(),
        clock=FrozenClock(NOW),
        recorder=recorder,
    )
    runs = StubRunManager(state)
    durable = DurableVerificationCoordinator(
        run_manager=runs,
        verification_service=service,
        operation_manager=operations,
        artifact_store=artifacts,
        observation_recorder=recorder,
        checkpoint_manager=checkpoint_manager,
    )
    return durable, runs, operations, trace


def _phase3_checkpoint_manager(*, complete: bool):
    example = runtime_example()
    fixtures = {
        ExecutionFixtureKey(task.task_id, 1): ExecutionFixture(
            result=successful_result(
                usage=RuntimeResourceAmount(
                    duration_milliseconds=1,
                    tokens=1,
                    cost_microunits=1,
                ),
                output_ids=tuple(
                    output.output_id for output in task.expected_outputs
                ),
            )
        )
        for task in example.dag.tasks
    }
    executor, manager, _, _, _ = build_executor(
        example, MockTaskExecutionBackend(fixtures)
    )
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    if complete:
        asyncio.run(executor.execute(example.state))
    return example, manager


def test_durable_verification_completes_authority_then_evaluating() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, runs, operations, trace = _durable(state, service, artifacts)

    result = asyncio.run(
        durable.execute(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    operation = operations.load(state.run_id)
    assert result == artifacts.load(state.run_id)
    assert operation.status is VerificationOperationStatus.COMPLETED
    assert operation.authority_content_hash == result.artifact_content_hash
    assert operation.lineage is not None
    assert runs.state.status is RunStatus.EVALUATING
    assert runs.transitions == [RunStatus.EVALUATING]
    assert len(model.requests) == 4
    assert trace.read(state.run_id)
    replay = asyncio.run(
        durable.recover(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert replay == result
    assert operations.load(state.run_id).recovery_attempts == 0
    assert len(model.requests) == 4


def test_legacy_authority_bootstrap_uses_zero_model_calls() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    authority = asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert len(model.requests) == 4

    durable, runs, operations, _ = _durable(state, service, artifacts)
    replay = asyncio.run(
        durable.recover(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert replay == authority
    assert len(model.requests) == 4
    assert runs.state.status is RunStatus.EVALUATING
    assert operations.load(state.run_id).status is (
        VerificationOperationStatus.COMPLETED
    )


def test_authority_after_checkpoint_lag_recovers_without_model_call() -> None:
    state, policy, frozen, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    operation_store = InMemoryVerificationOperationStore()
    trace = InMemoryTraceSink()
    recorder = ObservationRecorder(trace_sink=trace, clock=FrozenClock(NOW))
    operations = VerificationOperationManager(
        store=operation_store, clock=FrozenClock(NOW), recorder=recorder
    )
    verification_id = service.prepare_invocation(
        run_state=state, policy=policy
    )[1]
    operations.create(
        frozen=frozen,
        policy=policy,
        hard_limits=HARD_LIMITS,
        model_bundle_hash=service.model_bundle_hash,
        verification_id=verification_id,
    )
    authority = asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
            frozen_input=frozen,
            call_journal=operations.session(state.run_id),
            event_sink=lambda event: operations.record_event(
                state.run_id, event, critical=False
            ),
        )
    )
    assert operations.load(state.run_id).status is (
        VerificationOperationStatus.READY_TO_PUBLISH
    )
    assert len(model.requests) == 4

    durable, runs, operations, _ = _durable(
        state,
        service,
        artifacts,
        trace=trace,
        operation_store=operation_store,
    )
    recovered = asyncio.run(
        durable.recover(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert recovered == authority
    assert len(model.requests) == 4
    assert runs.state.status is RunStatus.EVALUATING
    assert operations.load(state.run_id).status is VerificationOperationStatus.COMPLETED


def test_legacy_authority_model_pin_mismatch_fails_before_model_call() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    original = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, original, artifacts)
    asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    different = MockVerificationModel({}, bundle_version="other")
    mismatched = VerificationService(
        claim_store=claim_store,
        evidence_store=evidence_store,
        artifact_store=artifacts,
        coordinator=VerificationCoordinator(model=different, clock=FrozenClock(NOW)),
        clock=FrozenClock(NOW),
        trace_sink=InMemoryTraceSink(),
    )
    durable, _, _, _ = _durable(state, mismatched, artifacts)
    with pytest.raises(VerificationRecoveryInconsistency, match="policy/model"):
        asyncio.run(
            durable.recover(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert different.requests == []


def test_capacity_preflight_rejects_before_running_to_verifying(
    monkeypatch,
) -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    state = state.model_copy(update={"status": RunStatus.RUNNING})
    policy = policy.model_copy(update={"max_rounds": 10})
    service, _ = build_service(
        evidence_store,
        claim_store,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    artifacts = InMemoryVerificationArtifactStore()
    durable, runs, _, _ = _durable(state, service, artifacts)
    monkeypatch.setattr(
        "researchos.domain.verification_runtime.RESERVED_CRITICAL_SLOTS", 136
    )
    with pytest.raises(ValueError, match="capacity"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert runs.transitions == []
    assert runs.state.status is RunStatus.RUNNING


def test_phase3_nonterminal_checkpoint_blocks_running_to_verifying() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    state = state.model_copy(update={"status": RunStatus.RUNNING})
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    _, checkpoint_manager = _phase3_checkpoint_manager(complete=False)
    durable, runs, operations, _ = _durable(
        state,
        service,
        artifacts,
        checkpoint_manager=checkpoint_manager,
    )

    with pytest.raises(VerificationPreconditionError, match="safely terminal"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert runs.state.status is RunStatus.RUNNING
    assert runs.transitions == []
    assert operations.list_generations(state.run_id) == ()
    assert model.requests == []


def test_phase3_checkpoint_identity_mismatch_blocks_without_mutation() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    state = state.model_copy(update={"status": RunStatus.RUNNING})
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    example, checkpoint_manager = _phase3_checkpoint_manager(complete=True)
    checkpoint = checkpoint_manager.load_and_reconcile(example.state.run_id)

    class MismatchedCheckpointManager:
        def load_and_reconcile(self, run_id):
            assert run_id == checkpoint.run_id
            return checkpoint.model_copy(
                update={"run_revision": checkpoint.run_revision + 1}
            )

    durable, runs, operations, _ = _durable(
        state,
        service,
        artifacts,
        checkpoint_manager=MismatchedCheckpointManager(),
    )
    with pytest.raises(VerificationRecoveryInconsistency, match="identity differs"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert runs.state.status is RunStatus.RUNNING
    assert runs.transitions == []
    assert operations.list_generations(state.run_id) == ()
    assert model.requests == []


@pytest.mark.parametrize(
    ("cause", "error_type", "call_status", "operation_status", "event_type"),
    [
        (
            "cancel",
            VerificationCancelled,
            ModelCallStatus.CANCELLED,
            VerificationOperationStatus.CANCELLED,
            TraceEventType.VERIFICATION_CANCELLED,
        ),
        (
            "deadline",
            VerificationDeadlineExceeded,
            ModelCallStatus.TIMED_OUT,
            VerificationOperationStatus.INTERRUPTED_UNKNOWN,
            TraceEventType.VERIFICATION_MODEL_CALL_INTERRUPTED_UNKNOWN,
        ),
    ],
)
def test_dispatched_cancellation_and_deadline_are_durably_unknown(
    cause, error_type, call_status, operation_status, event_type
) -> None:
    state, policy, frozen, _, _, _ = prepared()
    trace = InMemoryTraceSink()
    recorder = ObservationRecorder(trace_sink=trace, clock=FrozenClock(NOW))
    operations = VerificationOperationManager(
        store=InMemoryVerificationOperationStore(),
        clock=FrozenClock(NOW),
        recorder=recorder,
    )
    model = NeverVerificationModel()
    verification_id = __import__(
        "researchos.application.verification_input",
        fromlist=["compute_verification_id"],
    ).compute_verification_id(frozen, policy, model.model_bundle_hash)
    operations.create(
        frozen=frozen,
        policy=policy,
        hard_limits=HARD_LIMITS,
        model_bundle_hash=model.model_bundle_hash,
        verification_id=verification_id,
    )
    controller = AsyncioRunCancellationController()

    async def invoke():
        if cause == "cancel":
            async def cancel_soon():
                await asyncio.sleep(0.01)
                controller.request_cancel()

            asyncio.create_task(cancel_soon())
            deadline = None
        else:
            deadline = NOW + timedelta(milliseconds=5)
        return await VerificationCoordinator(
            model=model, clock=FrozenClock(NOW)
        ).execute(
            verification_id=verification_id,
            frozen=frozen,
            policy=policy,
            hard_limits=HARD_LIMITS,
            deadline=deadline,
            cancellation=controller.signal_for_attempt(),
            call_journal=operations.session(state.run_id),
        )

    with pytest.raises(error_type):
        asyncio.run(invoke())
    operation = operations.load(state.run_id)
    assert operation.status is operation_status
    assert operation.model_calls[0].status is call_status
    assert operation.model_calls[0].usage_certainty is UsageCertainty.UNKNOWN
    assert any(item.event_type is event_type for item in trace.read(state.run_id))


def test_evaluating_without_authority_is_recovery_inconsistency() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    state = state.model_copy(
        update={"status": RunStatus.EVALUATING, "revision": state.revision + 1}
    )
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(
        evidence_store, claim_store, MockVerificationModel(fixtures), artifacts
    )
    durable, _, _, _ = _durable(state, service, artifacts)
    with pytest.raises(VerificationRecoveryInconsistency, match="lacks"):
        asyncio.run(
            durable.recover(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )


def test_real_run_manager_owns_running_verifying_evaluating_transitions() -> None:
    _, policy, _, evidence_store, claim_store, old_fixtures = prepared()
    run_store = InMemoryRunStore()
    trace = InMemoryTraceSink()
    clock = FrozenClock(NOW)
    run_manager = RunManager(
        store=run_store,
        trace_sink=trace,
        clock=clock,
        id_factory=SequentialIds(),
    )
    state = run_manager.create(
        RunInput(query="runtime query"),
        RunConfig(allowed_capability_ids=("search", "read")),
    )
    for status in (RunStatus.PLANNING, RunStatus.READY, RunStatus.RUNNING):
        state = run_manager.transition(state.run_id, status)
    frozen = freeze_verification_input(
        run_id=state.run_id,
        run_revision=state.revision + 1,
        claim_store=claim_store,
        evidence_store=evidence_store,
        policy=policy,
    )
    model_hash = MockVerificationModel({}).model_bundle_hash
    verification_id = compute_verification_id(frozen, policy, model_hash)
    synthesis_id = stable_id("syn", [verification_id, "initial"])
    old_candidate = next(
        fixture.payload
        for key, fixture in old_fixtures.items()
        if key.role is VerificationRole.SYNTHESIZER
    )
    candidate = SynthesisCandidate.model_validate(old_candidate)
    helper = VerificationCoordinator(
        model=MockVerificationModel({}), clock=clock
    )
    initial = helper._initial_draft(synthesis_id, candidate, frozen, policy)
    revised, _, _ = helper._apply_blue(
        verification_id, initial, BlueResponse(), frozen, (), 1
    )
    fixtures = {
        VerificationFixtureKey(
            verification_id, VerificationRole.SYNTHESIZER, 0, "draft_pending"
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
    artifacts = InMemoryVerificationArtifactStore()
    model = MockVerificationModel(fixtures)
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    recorder = ObservationRecorder(trace_sink=trace, clock=clock)
    operations = VerificationOperationManager(
        store=InMemoryVerificationOperationStore(),
        clock=clock,
        recorder=recorder,
    )
    _, checkpoint_manager = _phase3_checkpoint_manager(complete=True)
    durable = DurableVerificationCoordinator(
        run_manager=run_manager,
        verification_service=service,
        operation_manager=operations,
        artifact_store=artifacts,
        observation_recorder=recorder,
        checkpoint_manager=checkpoint_manager,
    )
    result = asyncio.run(
        durable.execute(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    final_state = run_manager.load(state.run_id)
    assert result.run_revision == state.revision + 1
    assert final_state.status is RunStatus.EVALUATING
    assert final_state.revision == state.revision + 2


def test_slow_exporter_does_not_consume_short_verification_deadline() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    config = state.config.model_copy(
        update={"deadline": NOW + timedelta(milliseconds=100)}
    )
    state = state.model_copy(
        update={"config": config, "config_hash": model_sha256(config)}
    )
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(
        evidence_store, claim_store, MockVerificationModel(fixtures), artifacts
    )

    async def scenario():
        exporter = MockObservationExporter(delay_seconds=5)
        dispatcher = ExporterDispatcher(
            exporters=(exporter,),
            policy=ExporterPolicy(export_timeout_ms=10_000),
        )
        trace = InMemoryTraceSink()
        recorder = ObservationRecorder(
            trace_sink=trace,
            clock=FrozenClock(NOW),
            dispatcher=dispatcher,
        )
        operations = VerificationOperationManager(
            store=InMemoryVerificationOperationStore(),
            clock=FrozenClock(NOW),
            recorder=recorder,
        )
        durable = DurableVerificationCoordinator(
            run_manager=StubRunManager(state),
            verification_service=service,
            operation_manager=operations,
            artifact_store=artifacts,
            observation_recorder=recorder,
        )
        started = time.perf_counter()
        result = await durable.execute(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
        elapsed = time.perf_counter() - started
        await dispatcher.close(drain=False)
        return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert result == artifacts.load(state.run_id)
    assert elapsed < 0.5


class FailNextPublicationStore(InMemoryVerificationArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    def publish(self, result, markdown, *, expected_prior_verification_id):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("crash before authority publish")
        return super().publish(
            result,
            markdown,
            expected_prior_verification_id=expected_prior_verification_id,
        )


def test_ready_candidate_crash_replays_exact_occurrence_after_clock_advance() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = FailNextPublicationStore()
    artifacts.fail_next = True
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, _, operations, trace = _durable(state, service, artifacts)

    with pytest.raises(RuntimeError, match="before authority"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    ready = operations.load(state.run_id)
    assert ready.status is VerificationOperationStatus.READY_TO_PUBLISH
    candidate = ready.publication_candidate
    assert candidate is not None
    original_bytes = candidate.model_dump_json().encode("utf-8")
    original_calls = len(model.requests)
    service._clock.advance(hours=2)

    recovered = asyncio.run(
        durable.recover(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    persisted = operations.load(state.run_id).publication_candidate
    assert persisted is not None
    assert persisted.model_dump_json().encode("utf-8") == original_bytes
    assert recovered == candidate.result
    assert recovered.completed_at == candidate.result.completed_at
    assert recovered.artifact_content_hash == candidate.artifact_content_hash
    assert artifacts.report_bytes(state.run_id) == candidate.markdown_text.encode(
        "utf-8"
    )
    assert len(model.requests) == original_calls


def test_recovery_hard_limits_mismatch_fails_before_replay_or_model_call() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = FailNextPublicationStore()
    artifacts.fail_next = True
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, _, operations, _ = _durable(state, service, artifacts)
    with pytest.raises(RuntimeError):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    before = operations.load(state.run_id)
    calls = len(model.requests)
    with pytest.raises(VerificationRecoveryInconsistency, match="compatibility"):
        asyncio.run(
            durable.recover(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS.model_copy(update={"tokens": 10}),
                cancellation=NeverCancelled(),
            )
        )
    after = operations.load(state.run_id)
    assert after.checkpoint_revision == before.checkpoint_revision
    assert after.recovery_attempts == before.recovery_attempts
    assert len(model.requests) == calls


def test_old_authority_can_be_superseded_and_new_generation_recovers() -> None:
    state, old_policy, _, evidence_store, claim_store, old_fixtures = prepared()
    new_policy = old_policy.model_copy(update={"max_findings": 99})
    *_, new_fixtures = prepared(new_policy)
    model = MockVerificationModel({**old_fixtures, **new_fixtures})
    artifacts = FailNextPublicationStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    old = asyncio.run(
        service.verify(
            run_state=state,
            policy=old_policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    artifacts.fail_next = True
    durable, _, operations, _ = _durable(state, service, artifacts)
    with pytest.raises(RuntimeError, match="before authority"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=new_policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
                expected_prior_verification_id=old.verification_id,
            )
        )
    frozen, new_id = service.prepare_invocation(run_state=state, policy=new_policy)
    pending = operations.load(state.run_id, new_id)
    assert pending.expected_prior_verification_id == old.verification_id
    calls = len(model.requests)
    recovered = asyncio.run(
        durable.recover(
            run_id=state.run_id,
            policy=new_policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert recovered.verification_id == new_id
    assert recovered.supersedes_verification_id == old.verification_id
    assert operations.load(state.run_id, new_id).status is (
        VerificationOperationStatus.COMPLETED
    )
    assert len(model.requests) == calls
    assert frozen.frozen_input_hash == recovered.frozen_input_hash


def test_known_malformed_response_terminalizes_operation_without_retry_loop() -> None:
    state, policy, frozen, evidence_store, claim_store, fixtures = prepared()
    synthesis_key = VerificationFixtureKey(
        compute_verification_id(
            frozen, policy, MockVerificationModel({}).model_bundle_hash
        ),
        VerificationRole.SYNTHESIZER,
        0,
        "draft_pending",
    )
    fixtures[synthesis_key] = VerificationFixture(payload=b"{")
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, _, operations, _ = _durable(state, service, artifacts)
    with pytest.raises(VerificationModelFailure, match="malformed"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    failed = operations.load(state.run_id)
    assert failed.status is VerificationOperationStatus.FAILED
    assert failed.model_calls[-1].status is ModelCallStatus.FAILED
    attempts = failed.recovery_attempts
    calls = len(model.requests)
    with pytest.raises(VerificationRecoveryInconsistency, match="terminal"):
        asyncio.run(
            durable.recover(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert operations.load(state.run_id).recovery_attempts == attempts
    assert len(model.requests) == calls


def test_terminal_run_matching_authority_is_idempotent_read_only_recovery() -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, runs, operations, _ = _durable(state, service, artifacts)
    authority = asyncio.run(
        durable.execute(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    runs.state = RunState.model_validate(
        {
            **runs.state.model_dump(mode="python"),
            "revision": runs.state.revision + 1,
            "status": RunStatus.COMPLETED,
            "completed_at": NOW + timedelta(seconds=1),
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    transitions = tuple(runs.transitions)
    calls = len(model.requests)
    operation_revision = operations.load(
        state.run_id, authority.verification_id
    ).checkpoint_revision
    replay = asyncio.run(
        durable.recover(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert replay == authority
    assert tuple(runs.transitions) == transitions
    assert len(model.requests) == calls
    assert operations.load(
        state.run_id, authority.verification_id
    ).checkpoint_revision == operation_revision


def test_matching_completed_authority_reconciles_pending_outbox_best_effort() -> None:
    class ToggleTrace(InMemoryTraceSink):
        fail = True

        def append_once(self, descriptor):
            if self.fail:
                raise OSError("trace unavailable")
            return super().append_once(descriptor)

    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    trace = ToggleTrace()
    durable, runs, operations, _ = _durable(
        state, service, artifacts, trace=trace
    )
    authority = asyncio.run(
        durable.execute(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert any(
        not item.delivered
        for item in operations.load(state.run_id, authority.verification_id).outbox
    )
    runs.state = RunState.model_validate(
        {
            **runs.state.model_dump(mode="python"),
            "revision": runs.state.revision + 1,
            "status": RunStatus.COMPLETED,
            "completed_at": NOW + timedelta(seconds=1),
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    transitions = tuple(runs.transitions)
    calls = len(model.requests)
    trace.fail = False

    replay = asyncio.run(
        durable.recover(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert replay == authority
    assert tuple(runs.transitions) == transitions
    assert len(model.requests) == calls
    assert all(
        item.delivered
        for item in operations.load(state.run_id, authority.verification_id).outbox
    )


def test_contradicts_edge_cannot_establish_citation_support_lineage() -> None:
    state, policy, frozen, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    result = asyncio.run(
        service.verify(
            run_state=state,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    edge = frozen.current_valid_edges[0]
    contradicts = edge.model_copy(
        update={
            "revision": edge.revision.model_copy(
                update={"relation": ClaimEvidenceRelation.CONTRADICTS}
            )
        }
    )
    changed = frozen.model_copy(update={"current_valid_edges": (contradicts,)})
    with pytest.raises(VerificationRecoveryInconsistency, match="SUPPORTS"):
        reconstruct_verification_lineage(result, changed)


def test_trace_failure_after_dispatched_checkpoint_does_not_prevent_provider() -> None:
    class FailDispatchedTrace(InMemoryTraceSink):
        failed = False

        def append_once(self, descriptor):
            if (
                not self.failed
                and descriptor.event_type
                is TraceEventType.VERIFICATION_MODEL_CALL_DISPATCHED
            ):
                self.failed = True
                raise OSError("trace unavailable")
            return super().append_once(descriptor)

    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    trace = FailDispatchedTrace()
    durable, _, operations, _ = _durable(
        state, service, artifacts, trace=trace
    )
    result = asyncio.run(
        durable.execute(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    assert result == artifacts.load(state.run_id)
    assert len(model.requests) == 4
    assert sum(
        request.role is VerificationRole.SYNTHESIZER for request in model.requests
    ) == 1
    assert any(
        item.descriptor.event_type
        is TraceEventType.VERIFICATION_MODEL_CALL_DISPATCHED
        and not item.delivered
        for item in operations.load(state.run_id).outbox
    )


def test_provider_transport_exception_is_durably_interrupted_unknown() -> None:
    state, policy, _, evidence_store, claim_store, _ = prepared()

    class TransportFailureModel:
        model_bundle_hash = MockVerificationModel({}).model_bundle_hash

        def __init__(self) -> None:
            self.requests = []

        async def invoke(self, request, cancellation):
            del cancellation
            self.requests.append(request)
            raise ConnectionResetError("transport reset")

    model = TransportFailureModel()
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, _, operations, trace = _durable(state, service, artifacts)

    with pytest.raises(VerificationUsageUncertain, match="outcome is unknown"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    operation = operations.load(state.run_id)
    assert operation.status is VerificationOperationStatus.INTERRUPTED_UNKNOWN
    assert operation.model_calls[-1].status is ModelCallStatus.INTERRUPTED_UNKNOWN
    assert operation.model_calls[-1].usage_certainty is UsageCertainty.UNKNOWN
    assert operation.model_calls[-1].terminal_usage is None
    terminal_types = tuple(item.event_type for item in trace.read(state.run_id))
    assert TraceEventType.VERIFICATION_MODEL_CALL_INTERRUPTED_UNKNOWN in terminal_types
    assert TraceEventType.VERIFICATION_FAILED not in terminal_types
    calls = len(model.requests)
    with pytest.raises(VerificationRecoveryInconsistency, match="terminal"):
        asyncio.run(
            durable.recover(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert len(model.requests) == calls


@pytest.mark.parametrize(
    ("cause", "expected_status", "error_type"),
    [
        (
            "deadline",
            VerificationOperationStatus.FAILED,
            VerificationDeadlineExceeded,
        ),
        (
            "context",
            VerificationOperationStatus.FAILED,
            VerificationContractError,
        ),
        (
            "cancellation",
            VerificationOperationStatus.CANCELLED,
            VerificationCancelled,
        ),
    ],
)
def test_predispatch_failure_terminalizes_once(cause, expected_status, error_type):
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    if cause == "deadline":
        config = state.config.model_copy(
            update={"deadline": NOW - timedelta(milliseconds=1)}
        )
        state = state.model_copy(
            update={"config": config, "config_hash": model_sha256(config)}
        )
    elif cause == "context":
        # Frozen context fits, but the typed model request wrapper does not.
        policy = policy.model_copy(update={"max_model_context_bytes": 2_400})

    class AlreadyCancelled:
        cancelled = True

        async def wait(self):
            return None

    cancellation = AlreadyCancelled() if cause == "cancellation" else NeverCancelled()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, _, operations, _ = _durable(state, service, artifacts)

    with pytest.raises(error_type):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=cancellation,
            )
        )
    terminal = operations.load(state.run_id)
    assert terminal.status is expected_status
    assert model.requests == []
    attempts = terminal.recovery_attempts
    with pytest.raises(VerificationRecoveryInconsistency, match="terminal"):
        asyncio.run(
            durable.recover(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=cancellation,
            )
        )
    assert operations.load(state.run_id).recovery_attempts == attempts


def test_pending_generation_blocks_changed_policy_target() -> None:
    state, policy, frozen, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, _, operations, _ = _durable(state, service, artifacts)
    verification_id = compute_verification_id(
        frozen, policy, service.model_bundle_hash
    )
    operations.create(
        frozen=frozen,
        policy=policy,
        hard_limits=HARD_LIMITS,
        model_bundle_hash=service.model_bundle_hash,
        verification_id=verification_id,
    )
    changed_policy = policy.model_copy(
        update={"max_model_response_bytes": policy.max_model_response_bytes - 1}
    )

    with pytest.raises(VerificationRecoveryInconsistency, match="unfinished"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=changed_policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert len(operations.list_generations(state.run_id)) == 1
    assert model.requests == []


def test_invalid_predispatch_request_contract_terminalizes_failed(
    monkeypatch,
) -> None:
    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    monkeypatch.setattr(service._coordinator, "_base_context", lambda frozen: [])
    durable, _, operations, _ = _durable(state, service, artifacts)

    with pytest.raises(ValidationError):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    operation = operations.load(state.run_id)
    assert operation.status is VerificationOperationStatus.FAILED
    assert operation.last_error_code == "verification_request_contract_invalid"
    assert model.requests == []


def test_pending_generation_blocks_changed_claim_snapshot() -> None:
    state, policy, frozen, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, _, operations, trace = _durable(state, service, artifacts)
    verification_id = compute_verification_id(
        frozen, policy, service.model_bundle_hash
    )
    operations.create(
        frozen=frozen,
        policy=policy,
        hard_limits=HARD_LIMITS,
        model_bundle_hash=service.model_bundle_hash,
        verification_id=verification_id,
    )
    ClaimGraphService(
        store=claim_store,
        evidence_store=evidence_store,
        clock=FrozenClock(NOW),
        trace_sink=trace,
    ).create_claim(
        run_id=state.run_id,
        run_revision=state.revision,
        claim_scope_key="phase8_changed_scope",
        statement="A newly frozen claim changes the verification target.",
    )

    with pytest.raises(VerificationRecoveryInconsistency, match="unfinished"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert len(operations.list_generations(state.run_id)) == 1
    assert model.requests == []


def test_interrupted_unknown_generation_cannot_be_bypassed() -> None:
    state, policy, frozen, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    durable, _, operations, _ = _durable(state, service, artifacts)
    verification_id = compute_verification_id(
        frozen, policy, service.model_bundle_hash
    )
    operations.create(
        frozen=frozen,
        policy=policy,
        hard_limits=HARD_LIMITS,
        model_bundle_hash=service.model_bundle_hash,
        verification_id=verification_id,
    )
    prepared_call, replay = operations.session(
        state.run_id, verification_id
    ).prepare(
        VerificationModelRequest(
            verification_id=verification_id,
            role=VerificationRole.SYNTHESIZER,
            round_number=0,
            draft_revision_id="draft_pending",
            context={"frozen_input_hash": frozen.frozen_input_hash},
        )
    )
    assert replay is None
    operations.mark_dispatched(
        state.run_id, prepared_call, verification_id=verification_id
    )
    operations.begin_recovery(state.run_id, verification_id)
    assert (
        operations.load(state.run_id, verification_id).model_calls[-1].status
        is ModelCallStatus.INTERRUPTED_UNKNOWN
    )
    changed_policy = policy.model_copy(
        update={"max_model_response_bytes": policy.max_model_response_bytes - 1}
    )

    with pytest.raises(VerificationRecoveryInconsistency, match="unfinished"):
        asyncio.run(
            durable.execute(
                run_id=state.run_id,
                policy=changed_policy,
                hard_limits=HARD_LIMITS,
                cancellation=NeverCancelled(),
            )
        )
    assert len(operations.list_generations(state.run_id)) == 1
    assert model.requests == []


def test_delivered_flag_cas_conflict_is_reconciled_without_duplicate_trace() -> None:
    class FailDispatchedDeliveryStore(InMemoryVerificationOperationStore):
        failed = False

        def save(self, operation, *, expected_revision):
            current = self.load(operation.run_id, operation.verification_id)
            newly_delivered = [
                proposed
                for old, proposed in zip(
                    current.outbox, operation.outbox, strict=False
                )
                if not old.delivered and proposed.delivered
            ]
            if (
                not self.failed
                and any(
                    item.descriptor.event_type
                    is TraceEventType.VERIFICATION_MODEL_CALL_DISPATCHED
                    for item in newly_delivered
                )
            ):
                self.failed = True
                raise VerificationOperationRevisionConflict("injected CAS conflict")
            return super().save(operation, expected_revision=expected_revision)

    state, policy, _, evidence_store, claim_store, fixtures = prepared()
    model = MockVerificationModel(fixtures)
    artifacts = InMemoryVerificationArtifactStore()
    service, _ = build_service(evidence_store, claim_store, model, artifacts)
    store = FailDispatchedDeliveryStore()
    trace = InMemoryTraceSink()
    durable, _, operations, _ = _durable(
        state,
        service,
        artifacts,
        trace=trace,
        operation_store=store,
    )
    asyncio.run(
        durable.execute(
            run_id=state.run_id,
            policy=policy,
            hard_limits=HARD_LIMITS,
            cancellation=NeverCancelled(),
        )
    )
    before = tuple(
        item.event_id
        for item in trace.read(state.run_id)
        if item.event_type is TraceEventType.VERIFICATION_MODEL_CALL_DISPATCHED
    )
    assert len(before) == len(set(before)) == 4
    operations.reconcile_outbox(state.run_id)
    after = tuple(
        item.event_id
        for item in trace.read(state.run_id)
        if item.event_type is TraceEventType.VERIFICATION_MODEL_CALL_DISPATCHED
    )
    assert after == before
