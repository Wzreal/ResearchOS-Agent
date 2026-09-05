"""Official filesystem-backed composition boundary for Phase 10 workflows."""

from __future__ import annotations

from pathlib import Path

from researchos.adapters.checkpoint_filesystem import FilesystemCheckpointStore
from researchos.adapters.claim_extraction_operation_filesystem import (
    FilesystemClaimExtractionOperationStore,
)
from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
from researchos.adapters.clock import SystemClock
from researchos.adapters.evaluation_filesystem import FilesystemEvaluationArtifactStore
from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.adapters.sleeper import AsyncioRunCancellationController
from researchos.adapters.verification_filesystem import (
    FilesystemVerificationArtifactStore,
)
from researchos.adapters.verification_operation_filesystem import (
    FilesystemVerificationOperationStore,
)
from researchos.adapters.workflow_handoff_filesystem import (
    FilesystemWorkflowRuntimeHandoffStore,
)
from researchos.application.agent_runner import AgentRunner
from researchos.application.agent_task_backend import AgentTaskExecutionBackend
from researchos.application.async_dag_executor import AsyncDAGExecutor
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.checkpoint_manager import CheckpointManager
from researchos.application.claim_extraction_operation import (
    ClaimExtractionOperationManager,
)
from researchos.application.claim_extractor import ClaimExtractor
from researchos.application.claim_graph import ClaimGraphService
from researchos.application.dag_validator import DAGValidator
from researchos.application.durable_verification import DurableVerificationCoordinator
from researchos.application.evaluation_artifacts import ReadOnlyRunArtifactReader
from researchos.application.evaluation_evaluators import DeterministicArtifactEvaluator
from researchos.application.evaluation_harness import EvaluationHarness
from researchos.application.evidence_memory import EvidenceMemory
from researchos.application.execution_policy_builder import ExecutionPolicyBuilder
from researchos.application.observation_recorder import ObservationRecorder
from researchos.application.perspective_planner import PerspectivePlanner
from researchos.application.run_manager import RunManager
from researchos.application.verification_coordinator import VerificationCoordinator
from researchos.application.verification_operation import VerificationOperationManager
from researchos.application.verification_service import VerificationService
from researchos.application.workflow_coordinator import WorkflowCoordinator
from researchos.application.workflow_evaluation import structural_selfcheck_loader
from researchos.application.workflow_handoff import WorkflowHandoffManager
from researchos.configuration.phase10_mock import Phase10MockWorkflowBundleV1
from researchos.configuration.phase10_mock_runtime import build_phase10_mock_runtime
from researchos.domain.tools import AdapterMode


class WorkflowFactory:
    """Bind Phase 10's new durable artifacts to one filesystem output root.

    Existing Phase 1--9 services are injected: this class deliberately does not
    manufacture an alternate lifecycle, executor, evidence, or evaluation store.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def workflow_handoffs(
        self, *, runs, executor, clock, trace_sink
    ) -> WorkflowHandoffManager:
        if trace_sink is None:
            raise ValueError("Phase 10 filesystem factory requires a trace sink")
        return WorkflowHandoffManager(
            store=FilesystemWorkflowRuntimeHandoffStore(self.root),
            runs=runs,
            executor=executor,
            clock=clock,
            trace_sink=trace_sink,
        )

    def claim_extraction_operations(
        self, *, clock, trace_sink
    ) -> ClaimExtractionOperationManager:
        if trace_sink is None:
            raise ValueError("Phase 10 filesystem factory requires a trace sink")
        return ClaimExtractionOperationManager(
            store=FilesystemClaimExtractionOperationStore(self.root), clock=clock
        )

    def coordinator(self, **kwargs) -> WorkflowCoordinator:
        """Create the coordinator only after callers supplied existing authorities."""
        if kwargs.get("trace_sink") is None:
            raise ValueError("Phase 10 filesystem factory requires a trace sink")
        return WorkflowCoordinator(**kwargs)

    def build_mock(
        self,
        *,
        bundle: Phase10MockWorkflowBundleV1,
        query: str,
        clock=None,
        id_factory=None,
    ) -> WorkflowCoordinator:
        """Compose the one explicit fixture bundle through existing authorities."""

        # Reject model_construct() or otherwise tampered inputs before they can
        # select a runtime adapter.  The bundle is the sole configuration
        # authority for the official MOCK composition.
        bundle = Phase10MockWorkflowBundleV1.model_validate(
            bundle.model_dump(mode="python")
        )
        runtime = build_phase10_mock_runtime(bundle, query)
        clock = clock or SystemClock()
        trace = FilesystemTraceSink(self.root)
        runs = RunManager(
            store=FilesystemRunStore(self.root),
            trace_sink=trace,
            clock=clock,
            id_factory=id_factory,
        )
        evidence_store = FilesystemEvidenceStore(self.root)
        evidence = EvidenceMemory(store=evidence_store, clock=clock, trace_sink=trace)
        graph = ClaimGraphService(
            store=FilesystemClaimGraphStore(self.root),
            evidence_store=evidence_store,
            clock=clock,
            trace_sink=trace,
        )
        registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
        for tool in runtime.tools:
            registry.register(tool)
        runner = AgentRunner(
            agent=runtime.agent,
            registry=registry,
            policy=runtime.agent_policy,
            clock=clock,
            sleeper=runtime.runner_sleeper,
            trace_sink=trace,
            id_factory=id_factory,
        )
        checkpoints = CheckpointManager(
            store=FilesystemCheckpointStore(self.root),
            trace_sink=trace,
            clock=clock,
            id_factory=id_factory,
        )
        cancellation = AsyncioRunCancellationController()
        executor = AsyncDAGExecutor(
            checkpoints=checkpoints,
            backend=AgentTaskExecutionBackend(runner, evidence_ingestor=evidence),
            clock=clock,
            sleeper=runtime.executor_sleeper,
            cancellation=cancellation,
            id_factory=id_factory,
        )
        extraction_operations = self.claim_extraction_operations(
            clock=clock, trace_sink=trace
        )
        extractor = ClaimExtractor(
            operations=FilesystemClaimExtractionOperationStore(self.root),
            operation_manager=extraction_operations,
            evidence_store=evidence_store,
            graph=graph,
            model=runtime.claim_extraction_model,
            clock=clock,
            id_factory=id_factory,
        )
        observations = ObservationRecorder(trace_sink=trace, clock=clock)
        verification_artifacts = FilesystemVerificationArtifactStore(self.root)
        verification = DurableVerificationCoordinator(
            run_manager=runs,
            verification_service=VerificationService(
                claim_store=FilesystemClaimGraphStore(self.root),
                evidence_store=evidence_store,
                artifact_store=verification_artifacts,
                coordinator=VerificationCoordinator(
                    model=runtime.verification_model, clock=clock
                ),
                clock=clock,
                trace_sink=trace,
            ),
            operation_manager=VerificationOperationManager(
                store=FilesystemVerificationOperationStore(self.root),
                clock=clock,
                recorder=observations,
            ),
            artifact_store=verification_artifacts,
            observation_recorder=observations,
            checkpoint_manager=checkpoints,
        )
        return self.coordinator(
            runs=runs,
            planner=PerspectivePlanner(
                model=runtime.planning_model,
                validator=DAGValidator(),
                clock=clock,
                trace_sink=trace,
                id_factory=id_factory,
            ),
            execution_policy_builder=ExecutionPolicyBuilder(),
            handoffs=self.workflow_handoffs(
                runs=runs, executor=executor, clock=clock, trace_sink=trace
            ),
            executor=executor,
            evidence_store=evidence_store,
            claim_extractor=extractor,
            verification=verification,
            evaluation_harness=lambda state: EvaluationHarness(
                datasets=structural_selfcheck_loader(state, bundle.workflow_profile),
                artifacts=FilesystemEvaluationArtifactStore(self.root / "evaluations"),
                run_artifacts=ReadOnlyRunArtifactReader(
                    self.root, max_input_bytes_per_case=1_000_000
                ),
                clock=clock,
                evaluators=(DeterministicArtifactEvaluator(),),
            ),
            profile=bundle.workflow_profile,
            clock=clock,
            trace_sink=trace,
            cancellation=cancellation.signal_for_attempt(),
            mock_bundle_id=bundle.bundle_id,
            mock_bundle_version=bundle.bundle_version,
            mock_bundle_hash=bundle.bundle_hash,
        )
