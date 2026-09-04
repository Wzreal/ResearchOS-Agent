"""Deterministic filesystem release E2E using only public phase boundaries."""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import FrozenClock
from planning_fixtures import model_response, planning_policy

from researchos.adapters.checkpoint_filesystem import FilesystemCheckpointStore
from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
from researchos.adapters.evaluation_dataset import InMemoryEvaluationDatasetLoader
from researchos.adapters.evaluation_filesystem import FilesystemEvaluationArtifactStore
from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.adapters.mock_agent import (
    AgentFixture,
    AgentFixtureKey,
    ScriptedAgent,
)
from researchos.adapters.mock_planning import MockPlanningModel, PlanningFixtureKey
from researchos.adapters.mock_verification import (
    MockVerificationModel,
    VerificationFixture,
    VerificationFixtureKey,
)
from researchos.adapters.sleeper import (
    AsyncioRunCancellationController,
)
from researchos.adapters.verification_filesystem import (
    FilesystemVerificationArtifactStore,
)
from researchos.adapters.verification_operation_filesystem import (
    FilesystemVerificationOperationStore,
)
from researchos.application.agent_runner import AgentRunner, AgentRunnerPolicy
from researchos.application.agent_task_backend import AgentTaskExecutionBackend
from researchos.application.async_dag_executor import AsyncDAGExecutor
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.checkpoint_manager import CheckpointManager
from researchos.application.claim_graph import ClaimGraphService
from researchos.application.dag_validator import DAGValidator
from researchos.application.durable_verification import DurableVerificationCoordinator
from researchos.application.evaluation_artifacts import ReadOnlyRunArtifactReader
from researchos.application.evaluation_evaluators import DeterministicArtifactEvaluator
from researchos.application.evaluation_harness import EvaluationHarness
from researchos.application.evidence_memory import EvidenceMemory
from researchos.application.observation_recorder import ObservationRecorder
from researchos.application.perspective_planner import PerspectivePlanner
from researchos.application.run_manager import RunManager
from researchos.application.verification_coordinator import VerificationCoordinator
from researchos.application.verification_input import (
    compute_verification_id,
    freeze_verification_input,
)
from researchos.application.verification_operation import VerificationOperationManager
from researchos.application.verification_service import VerificationService
from researchos.domain.agent import (
    AgentDescriptor,
    AgentFinalDecision,
    AgentFinalResult,
    AgentProducedOutput,
    AgentToolCall,
    AgentToolDecision,
)
from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.contracts import RunConfig, RunInput, RunStatus
from researchos.domain.evaluation import (
    CaseRunBinding,
    DatasetProvenance,
    EvaluationCase,
    EvaluationDataset,
    EvaluationPolicy,
    EvaluationRequest,
    ReferenceLevel,
)
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.runtime import (
    ExecutionPolicy,
    IdempotencyMode,
    RuntimeResourceAmount,
    TaskRuntimePolicy,
    UsageCertainty,
)
from researchos.domain.synthesis import (
    BlueResponse,
    JudgeVerdict,
    SynthesisCandidate,
    SynthesisClaim,
    VerificationPolicy,
    VerificationRole,
)
from researchos.domain.tools import (
    AdapterMode,
    SearchHit,
    SearchRequest,
    SearchResult,
    ToolDescriptor,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)


class _NeverCancelled:
    @property
    def cancelled(self):
        return False

    async def wait(self):
        await asyncio.Future()


class _NeverSleeper:
    async def sleep(self, milliseconds: int) -> None:
        del milliseconds
        await asyncio.Future()


class _RuntimeSleeper:
    async def sleep(self, milliseconds: int) -> None:
        if milliseconds <= 0:
            await asyncio.sleep(0)
            return
        await asyncio.Future()


class _Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"e2e_{prefix}_{self.value}"


class _SearchTool:
    descriptor = ToolDescriptor(
        tool_id="search_tool",
        capability_id="search",
        adapter_id="offline_search",
        mode=AdapterMode.MOCK,
        operation_version="v1",
        input_type="search",
        output_type="search_result",
        side_effect=ToolSideEffect.NONE,
        idempotency=IdempotencyMode.IDEMPOTENT,
    )

    async def invoke(self, request, cancellation):
        del request, cancellation
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResult(
                adapter_id="offline_search",
                hits=(
                    SearchHit(
                        locator="source-a",
                        url="https://example.com/source-a",
                        title="Source A",
                        snippet="A deterministic supported fact.",
                        retrieved_at=FrozenClock().now(),
                        adapter_id="offline_search",
                    ),
                ),
            ),
            usage=ToolUsage(tool_calls=1),
            usage_certainty=UsageCertainty.EXACT,
        )


def _dataset(case: EvaluationCase) -> EvaluationDataset:
    values = dict(
        dataset_id="offline_release",
        dataset_version="1",
        provenance=DatasetProvenance(
            license_id="test",
            source_uri_hash="0" * 64,
            curator_id="tests",
            provenance_version="1",
        ),
        cases=(case,),
    )
    provisional = EvaluationDataset.model_construct(
        **values, dataset_content_hash="0" * 64
    )
    return EvaluationDataset(
        **values,
        dataset_content_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"dataset_content_hash"})
        ),
    )


def test_filesystem_offline_e2e_uses_only_legal_lifecycle_boundaries(tmp_path):
    clock = FrozenClock()
    ids = _Ids()
    trace = FilesystemTraceSink(tmp_path)
    runs = RunManager(
        store=FilesystemRunStore(tmp_path),
        trace_sink=trace,
        clock=clock,
        id_factory=ids,
    )
    state = runs.create(
        RunInput(query="offline end to end research"),
        RunConfig(allowed_capability_ids=("search", "read")),
    )
    assert state.status is RunStatus.CREATED
    state = runs.transition(state.run_id, RunStatus.PLANNING)

    planner = PerspectivePlanner(
        model=MockPlanningModel(
            {PlanningFixtureKey("offline end to end research", 0, None): model_response}
        ),
        validator=DAGValidator(),
        clock=clock,
        trace_sink=trace,
        id_factory=ids,
    )
    plan = planner.plan(state, planning_policy())
    assert plan.validated_dag is not None
    dag = plan.validated_dag
    state = runs.transition(state.run_id, RunStatus.READY)
    state = runs.transition(state.run_id, RunStatus.RUNNING)

    evidence_store = FilesystemEvidenceStore(tmp_path)
    evidence_memory = EvidenceMemory(
        store=evidence_store, clock=clock, trace_sink=trace
    )
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
    registry.register(_SearchTool())
    outputs = {task.task_id: task.expected_outputs[0] for task in dag.tasks}
    fixtures = {
        AgentFixtureKey("task_a", 1, 1): AgentFixture(
            decision=AgentToolDecision(
                tool_call=AgentToolCall(
                    tool_call_id="e2e_call",
                    capability_id="search",
                    input=SearchRequest(query="offline fact"),
                ),
                usage=RuntimeResourceAmount(),
                usage_certainty=UsageCertainty.EXACT,
            )
        ),
        AgentFixtureKey("task_a", 1, 2): AgentFixture(
            decision=AgentFinalDecision(
                final=AgentFinalResult(
                    outputs=(
                        AgentProducedOutput(
                            output_id=outputs["task_a"].output_id,
                            media_type=outputs["task_a"].media_type,
                        ),
                    )
                ),
                usage=RuntimeResourceAmount(),
                usage_certainty=UsageCertainty.EXACT,
            )
        ),
    }
    for task_id in ("task_b", "task_c"):
        fixtures[AgentFixtureKey(task_id, 1, 1)] = AgentFixture(
            decision=AgentFinalDecision(
                final=AgentFinalResult(
                    outputs=(
                        AgentProducedOutput(
                            output_id=outputs[task_id].output_id,
                            media_type=outputs[task_id].media_type,
                        ),
                    )
                ),
                usage=RuntimeResourceAmount(),
                usage_certainty=UsageCertainty.EXACT,
            )
        )
    runner = AgentRunner(
        agent=ScriptedAgent(
            AgentDescriptor(
                agent_id="offline_agent",
                adapter_id="scripted",
                mode=AdapterMode.MOCK,
                operation_version="v1",
            ),
            fixtures,
        ),
        registry=registry,
        policy=AgentRunnerPolicy(max_agent_steps=2, max_tool_calls=1),
        clock=clock,
        sleeper=_NeverSleeper(),
        trace_sink=trace,
        id_factory=ids,
    )
    task_policies = tuple(
        TaskRuntimePolicy(
            task_id=task.task_id,
            operation_version="v1",
            max_attempts=1,
            backoff_milliseconds=(),
            timeout_milliseconds=1_000,
            reservation=RuntimeResourceAmount(
                duration_milliseconds=1_000,
                tokens=100,
                cost_microunits=100,
                tool_calls=1,
            ),
            idempotency=IdempotencyMode.IDEMPOTENT,
            retry_on_timeout=True,
        )
        for task in dag.tasks
    )
    checkpoint_store = FilesystemCheckpointStore(tmp_path)
    checkpoints = CheckpointManager(
        store=checkpoint_store, trace_sink=trace, clock=clock, id_factory=ids
    )
    executor = AsyncDAGExecutor(
        checkpoints=checkpoints,
        backend=AgentTaskExecutionBackend(runner, evidence_ingestor=evidence_memory),
        clock=clock,
        sleeper=_RuntimeSleeper(),
        cancellation=AsyncioRunCancellationController(),
        id_factory=ids,
    )
    executor.initialize(
        state,
        dag,
        ExecutionPolicy(max_concurrency=2, tasks=task_policies, max_replans=1),
        replan_context=plan.replan_context,
    )
    summary = asyncio.run(executor.execute(state))
    persisted = checkpoint_store.load(state.run_id)
    assert summary.succeeded == 3, [
        (
            item.task_id,
            item.status.value,
            item.attempts[-1].failure_code if item.attempts else None,
        )
        for item in persisted.task_states
    ]

    evidence = evidence_store.load(state.run_id)
    assert len(evidence.evidence) == 1
    claim_store = FilesystemClaimGraphStore(tmp_path)
    graph = ClaimGraphService(
        store=claim_store, evidence_store=evidence_store, clock=clock, trace_sink=trace
    )
    claim = graph.create_claim(
        run_id=state.run_id,
        run_revision=state.revision,
        claim_scope_key="offline_fact",
        statement="The deterministic fact is supported.",
    )
    graph.relate(
        run_id=state.run_id,
        run_revision=state.revision,
        claim_id=claim.claim.claim_id,
        evidence_id=evidence.evidence[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )

    verification_policy = VerificationPolicy(max_rounds=1)
    # DurableVerificationCoordinator performs this legal RUNNING -> VERIFYING
    # transition itself. Predict its immutable input for exact mock fixtures;
    # no persisted state is changed outside RunManager.
    verifying_state = state.model_copy(
        update={"revision": state.revision + 1, "status": RunStatus.VERIFYING}
    )
    frozen = freeze_verification_input(
        run_id=state.run_id,
        run_revision=verifying_state.revision,
        claim_store=claim_store,
        evidence_store=evidence_store,
        policy=verification_policy,
    )
    empty_model = MockVerificationModel({})
    verification_id = compute_verification_id(
        frozen, verification_policy, empty_model.model_bundle_hash
    )
    candidate = SynthesisCandidate(
        section_titles=("Findings",),
        claims=(
            SynthesisClaim(
                claim_id=claim.claim.claim_id,
                claim_revision=1,
                section_ordinal=1,
                prose="The deterministic fact is supported.",
                evidence_ids=(evidence.evidence[0].evidence_id,),
            ),
        ),
    )
    coordinator = VerificationCoordinator(model=empty_model, clock=clock)
    initial = coordinator._initial_draft(
        stable_id("syn", [verification_id, "initial"]),
        candidate,
        frozen,
        verification_policy,
    )
    revised, _, _ = coordinator._apply_blue(
        verification_id, initial, BlueResponse(), frozen, (), 1
    )
    model = MockVerificationModel(
        {
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
    )
    artifacts = FilesystemVerificationArtifactStore(tmp_path)
    service = VerificationService(
        claim_store=claim_store,
        evidence_store=evidence_store,
        artifact_store=artifacts,
        coordinator=VerificationCoordinator(model=model, clock=clock),
        clock=clock,
        trace_sink=trace,
    )
    recorder = ObservationRecorder(trace_sink=trace, clock=clock)
    operations = VerificationOperationManager(
        store=FilesystemVerificationOperationStore(tmp_path),
        clock=clock,
        recorder=recorder,
    )
    durable = DurableVerificationCoordinator(
        run_manager=runs,
        verification_service=service,
        operation_manager=operations,
        artifact_store=artifacts,
        observation_recorder=recorder,
        checkpoint_manager=checkpoints,
    )
    result = asyncio.run(
        durable.execute(
            run_id=state.run_id,
            policy=verification_policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=1_000,
                cost_microunits=1_000,
                tool_calls=10,
            ),
            cancellation=_NeverCancelled(),
        )
    )
    assert (
        result.run_id == state.run_id
        and runs.load(state.run_id).status is RunStatus.EVALUATING
    )

    case = EvaluationCase(
        case_id="offline_case",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="offline end to end research",
    )
    dataset = _dataset(case)
    harness = EvaluationHarness(
        datasets=InMemoryEvaluationDatasetLoader((dataset,)),
        artifacts=FilesystemEvaluationArtifactStore(tmp_path / "evaluations"),
        run_artifacts=ReadOnlyRunArtifactReader(
            tmp_path, max_input_bytes_per_case=1_000_000
        ),
        clock=clock,
        evaluators=(DeterministicArtifactEvaluator(),),
    )
    evaluation = asyncio.run(
        harness.evaluate_existing(
            EvaluationRequest(
                dataset_id=dataset.dataset_id,
                dataset_version=dataset.dataset_version,
                dataset_hash=dataset.dataset_content_hash,
                bindings=(CaseRunBinding(case_id=case.case_id, run_id=state.run_id),),
                commit_sha="a" * 40,
                system_version="offline",
                policy=EvaluationPolicy(
                    policy_id="offline_e2e",
                    policy_version="1",
                    enabled_evaluator_ids=("deterministic_core",),
                    allow_nonterminal_runs=True,
                ),
                deadline=clock.now() + timedelta(minutes=1),
            ),
            _NeverCancelled(),
        )
    )
    assert evaluation.cases and runs.load(state.run_id).status is RunStatus.EVALUATING
    assert (
        runs.finalize(state.run_id, RunStatus.COMPLETED).status is RunStatus.COMPLETED
    )
