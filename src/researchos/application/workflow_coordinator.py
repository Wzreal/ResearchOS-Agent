"""Phase 10 durable orchestration over the existing application authorities."""

from __future__ import annotations

from collections.abc import Callable

from researchos.application.async_dag_executor import AsyncDAGExecutor
from researchos.application.claim_extractor import ClaimExtractor
from researchos.application.durable_verification import DurableVerificationCoordinator
from researchos.application.execution_policy_builder import ExecutionPolicyBuilder
from researchos.application.perspective_planner import PerspectivePlanner
from researchos.application.run_manager import RunManager
from researchos.application.workflow_budget import prepare_phase10_planning_admission
from researchos.application.workflow_evaluation import structural_selfcheck_request
from researchos.application.workflow_handoff import WorkflowHandoffManager
from researchos.domain.claim_extraction_operation import ClaimExtractionOperationStatus
from researchos.domain.contracts import (
    ErrorCategory,
    RunConfig,
    RunError,
    RunInput,
    RunState,
    RunStatus,
    TraceEventType,
    model_sha256,
)
from researchos.domain.identity import stable_id
from researchos.domain.planning import TaskDAG
from researchos.domain.runtime import ExecutorStatus
from researchos.domain.workflow import Phase10WorkflowProfileV1
from researchos.interfaces.evidence import EvidenceStore
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.interfaces.runtime import CancellationSignal


class WorkflowCoordinator:
    """Continue a workflow solely from Run and existing durable authorities."""

    def __init__(
        self,
        *,
        runs: RunManager,
        planner: PerspectivePlanner,
        execution_policy_builder: ExecutionPolicyBuilder,
        handoffs: WorkflowHandoffManager,
        executor: AsyncDAGExecutor,
        evidence_store: EvidenceStore,
        claim_extractor: ClaimExtractor,
        verification: DurableVerificationCoordinator,
        evaluation_harness: Callable[[RunState], object],
        profile: Phase10WorkflowProfileV1,
        clock: Clock,
        trace_sink: TraceSink,
        cancellation: CancellationSignal,
        mock_bundle_id: str | None = None,
        mock_bundle_version: str | None = None,
        mock_bundle_hash: str | None = None,
        planning_reservation: object | None = None,
        runtime_binder: Callable[[RunState], None] | None = None,
        evaluating_rebinder: Callable[[RunState], None] | None = None,
    ) -> None:
        self._runs = runs
        self._planner = planner
        self._policies = execution_policy_builder
        self._handoffs = handoffs
        self._executor = executor
        self._evidence = evidence_store
        self._extractor = claim_extractor
        self._verification = verification
        self._evaluation_harness = evaluation_harness
        self._profile = profile
        self._clock = clock
        self._trace = trace_sink
        self._cancellation = cancellation
        self._mock_bundle_id = mock_bundle_id
        self._mock_bundle_version = mock_bundle_version
        self._mock_bundle_hash = mock_bundle_hash
        self._planning_reservation = planning_reservation
        self._runtime_binder = runtime_binder
        self._evaluating_rebinder = evaluating_rebinder

    async def create_and_execute(
        self, run_input: RunInput, config: RunConfig
    ) -> RunState:
        return await self.execute(self._runs.create(run_input, config))

    async def resume_and_execute(
        self,
        run_id: str,
        *,
        expected_input: RunInput,
        expected_config: RunConfig,
    ) -> RunState:
        state = self._runs.load(run_id)
        if state.status in {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return state
        # A Phase 7 EvaluationRun freezes a manifest/trace fingerprint.  Do
        # not append RunManager's RESUMED event in the post-publication crash
        # window: evaluating the same request will load the published authority.
        if state.status is RunStatus.EVALUATING:
            if self._evaluating_rebinder is not None:
                self._evaluating_rebinder(state)
            return await self.execute(state)
        resumed = self._runs.resume(
            run_id, expected_input=expected_input, expected_config=expected_config
        )
        return await self.execute(resumed)

    async def execute(self, state: RunState) -> RunState:
        """Advance one Run; no stage cursor is retained on this object."""
        state = self._runs.load(state.run_id)
        if self._runtime_binder is not None:
            self._runtime_binder(state)
        if state.status in {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return state
        if self._cancellation.cancelled:
            return self._runs.finalize(
                state.run_id, RunStatus.CANCELLED, reason="workflow_cancelled"
            )
        if state.status is RunStatus.CREATED:
            state = self._runs.transition(state.run_id, RunStatus.PLANNING)
        if state.status is RunStatus.PLANNING:
            state = self._plan_or_recover(state)
        if state.status in {RunStatus.PLANNING, RunStatus.READY, RunStatus.RUNNING}:
            state = self._runs.load(state.run_id)
            if state.status is not RunStatus.RUNNING:
                self._handoffs.create_or_validate_checkpoint(state)
                state = self._runs.load(state.run_id)
            summary = await self._executor.execute(state)
            if summary.executor_status is ExecutorStatus.CANCELLED:
                return self._runs.finalize(
                    state.run_id, RunStatus.CANCELLED, reason="execution_cancelled"
                )
            if (
                summary.executor_status is not ExecutorStatus.COMPLETED
                or summary.failed
                or summary.blocked
            ):
                return self._runs.finalize(
                    state.run_id, RunStatus.PARTIAL, reason="execution_partial"
                )
            state = self._runs.load(state.run_id)
            if not self._evidence.load(state.run_id).evidence:
                return self._runs.finalize(
                    state.run_id, RunStatus.PARTIAL, reason="no_eligible_evidence"
                )
            extraction = self._extractor.execute(
                state,
                extraction_id=stable_id(
                    "extract", [state.run_id, self._profile.profile_hash]
                ),
                policy=self._profile.claim_extraction_policy,
                workflow_budget_slice_hash=model_sha256(
                    self._profile.budget.allocation.claim_extraction
                ),
                cancellation=self._cancellation,
            )
            if extraction.status is ClaimExtractionOperationStatus.CANCELLED:
                return self._runs.finalize(
                    state.run_id,
                    RunStatus.CANCELLED,
                    reason="claim_extraction_cancelled",
                )
            if extraction.status is not ClaimExtractionOperationStatus.COMPLETED:
                return self._runs.finalize(
                    state.run_id, RunStatus.PARTIAL, reason="claim_extraction_partial"
                )
        state = self._runs.load(state.run_id)
        if state.status in {RunStatus.RUNNING, RunStatus.VERIFYING}:
            await self._verification.execute(
                run_id=state.run_id,
                policy=self._profile.verification_policy,
                hard_limits=self._profile.budget.allocation.verification,
                cancellation=self._cancellation,
            )
            state = self._runs.load(state.run_id)
        if state.status is RunStatus.EVALUATING:
            request = structural_selfcheck_request(
                state, self._profile, clock=self._clock
            )
            harness = self._evaluation_harness(state)
            await harness.evaluate_existing(request, self._cancellation)
            return self._runs.finalize(state.run_id, RunStatus.COMPLETED)
        return self._runs.load(state.run_id)

    def _plan_or_recover(self, state: RunState) -> RunState:
        started = [
            event
            for event in self._trace.read(state.run_id, recover_torn_tail=True)
            if event.event_type is TraceEventType.PLANNING_STARTED
        ]
        if started:
            # A valid durable handoff is the only recoverable planning outcome.
            try:
                self._handoffs.create_or_validate_checkpoint(state)
            except Exception as exc:
                return self._runs.finalize(
                    state.run_id,
                    RunStatus.FAILED,
                    reason="planning_outcome_unknown",
                    errors=(
                        self._planning_failure_error(
                            state,
                            code=getattr(exc, "code", "planning_outcome_unknown"),
                            message=str(exc),
                            retryable=False,
                            category=ErrorCategory.LIFECYCLE,
                            details={"planning_recovery": "handoff_missing_or_invalid"},
                        ),
                    ),
                )
            return self._runs.load(state.run_id)
        admission = prepare_phase10_planning_admission(
            state,
            config=self._profile.budget,
            planning_reservation=self._planning_reservation,
            workflow_profile_hash=self._profile.profile_hash,
            mock_bundle_id=self._mock_bundle_id,
            mock_bundle_version=self._mock_bundle_version,
            mock_bundle_hash=self._mock_bundle_hash,
        )
        result = self._planner.plan(
            state, self._profile.planning_policy, phase10_planning_admission=admission
        )
        if result.validated_dag is None:
            planning_error = result.planning_error
            return self._runs.finalize(
                state.run_id,
                RunStatus.FAILED,
                reason="planning_not_validated",
                errors=(
                    self._planning_failure_error(
                        state,
                        code=(
                            planning_error.code
                            if planning_error is not None
                            else "planning_not_validated"
                        ),
                        message=(
                            planning_error.message
                            if planning_error is not None
                            else "planning did not produce a validated DAG"
                        ),
                        retryable=(
                            planning_error.retryable
                            if planning_error is not None
                            else False
                        ),
                        category=(
                            ErrorCategory.INTERNAL
                            if planning_error is not None
                            else ErrorCategory.VALIDATION
                        ),
                        details={
                            "planning_status": result.status.value,
                            "planning_request_id": result.planning_request_id,
                        },
                    ),
                ),
            )
        policy = self._policies.build(
            result.validated_dag,
            admission.allocation.execution,
            self._execution_config_for_dag(result.validated_dag),
        )
        self._handoffs.prepare(
            state,
            result,
            execution_policy=policy,
            allocation=admission.allocation,
            runtime_run_revision=state.revision + 2,
            workflow_profile_hash=self._profile.profile_hash,
            mock_bundle_id=self._mock_bundle_id,
            mock_bundle_version=self._mock_bundle_version,
            mock_bundle_hash=self._mock_bundle_hash,
        )
        self._handoffs.create_or_validate_checkpoint(state)
        return self._runs.load(state.run_id)

    def _execution_config_for_dag(self, dag: TaskDAG):
        """Bind existing uniform Phase 10 policy templates to validated task IDs."""

        config = self._profile.execution
        policies = tuple(sorted(config.task_policies, key=lambda item: item.task_id))
        tasks = tuple(sorted(dag.tasks, key=lambda item: item.task_id))
        if {item.task_id for item in policies} == {item.task_id for item in tasks}:
            return config
        # The planner is authoritative for accepted DAG cardinality.  Uniform
        # configuration entries are templates, not a second cardinality limit.
        # Materialization remains bounded by the immutable planning policy and
        # preserves ExecutionPolicyBuilder's exact task-ID coverage check.
        if not policies or len(tasks) > self._profile.planning_policy.max_tasks:
            return config
        template = policies[0]
        if any(
            item.model_dump(mode="python", exclude={"task_id"})
            != template.model_dump(mode="python", exclude={"task_id"})
            for item in policies[1:]
        ):
            return config
        return config.model_copy(
            update={
                "task_policies": tuple(
                    template.model_copy(update={"task_id": task.task_id})
                    for task in tasks
                )
            }
        )

    def _planning_failure_error(
        self,
        state: RunState,
        *,
        code: str,
        message: str,
        retryable: bool,
        category: ErrorCategory,
        details: dict[str, object],
    ) -> RunError:
        """Project the planner's typed failure into the existing Run error authority."""

        return RunError(
            error_id=stable_id("err", [state.run_id, "planning", code, message]),
            code=code,
            category=category,
            message=message,
            retryable=retryable,
            occurred_at=self._clock.now(),
            details=details,
        )
