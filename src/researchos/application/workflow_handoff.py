"""Durable bridge from a validated plan to the first Phase 3 checkpoint."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from researchos.application.async_dag_executor import AsyncDAGExecutor
from researchos.application.errors import (
    CheckpointAlreadyExists,
    CheckpointCompatibilityError,
    CheckpointNotFound,
    CorruptWorkflowHandoff,
    WorkflowHandoffAlreadyExists,
)
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import (
    RunState,
    RunStatus,
    TraceEventType,
    model_sha256,
)
from researchos.domain.planning import PlanningResult, PlanningStatus
from researchos.domain.runtime import (
    ExecutionPolicy,
    RuntimeCheckpoint,
    RuntimeResourceAmount,
)
from researchos.domain.workflow import (
    WorkflowBudgetAllocation,
    WorkflowHandoffStatus,
    WorkflowRuntimeHandoff,
)
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.interfaces.workflow import WorkflowRuntimeHandoffStore

IdFactory = Callable[[str], str]


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class WorkflowHandoffManager:
    """Own only handoff persistence and checkpoint creation/recovery."""

    def __init__(
        self,
        *,
        store: WorkflowRuntimeHandoffStore,
        runs: RunManager,
        executor: AsyncDAGExecutor,
        clock: Clock,
        trace_sink: TraceSink | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._store = store
        self._runs = runs
        self._executor = executor
        self._clock = clock
        self._trace = trace_sink
        self._id_factory = id_factory or _default_id_factory

    def prepare(
        self,
        state: RunState,
        result: PlanningResult,
        *,
        execution_policy: ExecutionPolicy,
        allocation: WorkflowBudgetAllocation,
        runtime_run_revision: int,
        workflow_profile_hash: str,
        mock_bundle_id: str | None = None,
        mock_bundle_version: str | None = None,
        mock_bundle_hash: str | None = None,
    ) -> WorkflowRuntimeHandoff:
        if state.status is not RunStatus.PLANNING:
            raise CorruptWorkflowHandoff("workflow handoff requires planning Run")
        if (
            result.status is not PlanningStatus.VALIDATED
            or result.validated_dag is None
        ):
            raise CorruptWorkflowHandoff("workflow handoff requires a validated plan")
        if result.run_id != state.run_id or result.run_revision != state.revision:
            raise CorruptWorkflowHandoff("validated plan does not match planning Run")
        if runtime_run_revision < state.revision:
            raise CorruptWorkflowHandoff("runtime revision precedes planning revision")
        self._validate_allocation(state, allocation)
        self._validate_planning_freeze(
            state,
            allocation,
            workflow_profile_hash,
            mock_bundle_id,
            mock_bundle_version,
            mock_bundle_hash,
        )
        handoff = WorkflowRuntimeHandoff.build(
            handoff_id=self._id_factory("handoff"),
            run_id=state.run_id,
            planning_run_revision=state.revision,
            runtime_run_revision=runtime_run_revision,
            dag=result.validated_dag,
            execution_policy=execution_policy,
            workflow_profile_hash=workflow_profile_hash,
            mock_bundle_id=mock_bundle_id,
            mock_bundle_version=mock_bundle_version,
            mock_bundle_hash=mock_bundle_hash,
            replan_context=result.replan_context,
            budget_allocation=allocation,
            created_at=self._clock.now(),
        )
        try:
            self._store.create(handoff)
        except WorkflowHandoffAlreadyExists as exc:
            existing = self._store.load(state.run_id)
            if existing != handoff:
                raise CorruptWorkflowHandoff(
                    "existing handoff differs from plan"
                ) from exc
            return existing
        return handoff

    def create_or_validate_checkpoint(self, state: RunState) -> RuntimeCheckpoint:
        handoff = self._store.load(state.run_id)
        self._validate_handoff_for_state(handoff, state)
        current = state
        if current.status is RunStatus.PLANNING:
            current = self._runs.transition(current.run_id, RunStatus.READY)
        if current.status is RunStatus.READY:
            current = self._runs.transition(current.run_id, RunStatus.RUNNING)
        if current.status is not RunStatus.RUNNING:
            raise CorruptWorkflowHandoff("handoff cannot create checkpoint here")
        if current.revision != handoff.runtime_run_revision:
            raise CorruptWorkflowHandoff("runtime Run revision differs from handoff")
        try:
            checkpoint = self._executor.resume(current)
        except CheckpointNotFound:
            try:
                checkpoint = self._executor.initialize(
                    current,
                    handoff.dag,
                    handoff.execution_policy,
                    replan_context=handoff.replan_context,
                    budget_limits=handoff.budget_allocation.execution,
                )
            except CheckpointAlreadyExists:
                checkpoint = self._executor.resume(current)
        self._validate_checkpoint(handoff, checkpoint)
        if handoff.status is WorkflowHandoffStatus.PREPARED:
            committed = handoff.model_copy(
                update={
                    "handoff_revision": handoff.handoff_revision + 1,
                    "status": WorkflowHandoffStatus.CHECKPOINT_COMMITTED,
                    "updated_at": self._clock.now(),
                }
            )
            self._store.save(committed, expected_revision=handoff.handoff_revision)
        return checkpoint

    @staticmethod
    def _validate_allocation(
        state: RunState, allocation: WorkflowBudgetAllocation
    ) -> None:
        total = RuntimeResourceAmount(
            duration_milliseconds=state.budget.limits.max_duration_seconds * 1_000,
            tokens=state.budget.limits.max_tokens,
            cost_microunits=state.budget.limits.max_cost_microunits,
            tool_calls=state.budget.limits.max_tool_calls,
        )
        if allocation.total != total:
            raise CorruptWorkflowHandoff("workflow allocation total differs from Run")

    def _validate_planning_freeze(
        self,
        state: RunState,
        allocation: WorkflowBudgetAllocation,
        workflow_profile_hash: str,
        mock_bundle_id: str | None,
        mock_bundle_version: str | None,
        mock_bundle_hash: str | None,
    ) -> None:
        """Bind a Phase 10 handoff to the already durable planning boundary.

        The optional trace dependency keeps all pre-Phase-10 callers exactly as
        they were.  A Phase 10 factory must provide it, making a missing or
        malformed freeze fail closed instead of reconstructing mutable policy.
        """

        if self._trace is None:
            return
        events = [
            event
            for event in self._trace.read(state.run_id, recover_torn_tail=True)
            if event.event_type is TraceEventType.PLANNING_STARTED
            and event.revision == state.revision
        ]
        if len(events) != 1:
            raise CorruptWorkflowHandoff(
                "Phase 10 handoff requires exactly one planning.started freeze"
            )
        attributes = events[0].attributes
        try:
            frozen = WorkflowBudgetAllocation.model_validate(
                attributes["workflow_budget_allocation"]
            )
            frozen_hash = attributes["workflow_budget_allocation_hash"]
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptWorkflowHandoff(
                "planning.started allocation freeze is missing or invalid"
            ) from exc
        if (
            frozen_hash != model_sha256(frozen)
            or frozen != allocation
            or attributes.get("workflow_profile_hash") != workflow_profile_hash
            or attributes.get("mock_bundle_id") != mock_bundle_id
            or attributes.get("mock_bundle_version") != mock_bundle_version
            or attributes.get("mock_bundle_hash") != mock_bundle_hash
        ):
            raise CorruptWorkflowHandoff(
                "workflow handoff allocation differs from planning.started freeze"
            )

    @staticmethod
    def _validate_handoff_for_state(
        handoff: WorkflowRuntimeHandoff, state: RunState
    ) -> None:
        if handoff.run_id != state.run_id:
            raise CorruptWorkflowHandoff("workflow handoff belongs to another Run")
        if (
            state.status is RunStatus.PLANNING
            and state.revision != handoff.planning_run_revision
        ):
            raise CorruptWorkflowHandoff("planning Run revision differs from handoff")
        if state.status in {RunStatus.READY, RunStatus.RUNNING} and (
            state.revision != handoff.runtime_run_revision
        ):
            raise CorruptWorkflowHandoff("runtime Run revision differs from handoff")

    @staticmethod
    def _validate_checkpoint(
        handoff: WorkflowRuntimeHandoff, checkpoint: RuntimeCheckpoint
    ) -> None:
        if (
            checkpoint.run_id != handoff.run_id
            or checkpoint.run_revision != handoff.runtime_run_revision
            or checkpoint.dag_hash != handoff.dag_hash
            or checkpoint.execution_policy_hash != handoff.execution_policy_hash
            or checkpoint.replan.context != handoff.replan_context
            or checkpoint.budget.limits != handoff.budget_allocation.execution
        ):
            raise CheckpointCompatibilityError("checkpoint differs from handoff")
        if (
            model_sha256(checkpoint.dag) != handoff.dag_hash
            or model_sha256(checkpoint.execution_policy)
            != handoff.execution_policy_hash
        ):
            raise CheckpointCompatibilityError("checkpoint semantic pins differ")
