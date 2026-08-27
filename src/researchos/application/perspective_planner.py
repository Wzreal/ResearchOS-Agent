"""Phase 2 planning orchestration without DAG execution or persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from researchos.application.dag_validator import DAGValidator
from researchos.application.errors import (
    PlanningModelFailure,
    PlanningPreconditionError,
    ReplanLineageConflict,
)
from researchos.domain.contracts import RunState, RunStatus, TraceEvent, TraceEventType
from researchos.domain.planning import (
    CandidatePlan,
    PlanningError,
    PlanningPolicy,
    PlanningRequest,
    PlanningResult,
    PlanningStatus,
    RemainingBudget,
    ReplanContext,
    ReplanDecision,
    ReplanDecisionStatus,
    ReplanPolicy,
    ReplanRequest,
    ReplanResult,
    ValidationResult,
)
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.interfaces.planning import PlanningModel
from researchos.security.redaction import PersistenceRedactor

IdFactory = Callable[[str], str]

_CANDIDATE_LOCATION_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "plan_id",
        "perspectives",
        "perspective_id",
        "name",
        "title",
        "goal",
        "rationale",
        "priority",
        "tasks",
        "task_id",
        "objective",
        "dependencies",
        "required_capability_ids",
        "expected_outputs",
        "output_id",
        "description",
        "media_type",
        "estimate",
        "duration_milliseconds",
        "tokens",
        "cost_microunits",
        "tool_calls",
        "policy",
        "required",
        "planner_metadata",
        "metadata_version",
        "planning_model_id",
        "planner_id",
        "planner_version",
    }
)


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class PerspectivePlanner:
    """Own one-shot planning and bounded in-memory replan lineages."""

    def __init__(
        self,
        *,
        model: PlanningModel,
        validator: DAGValidator,
        clock: Clock,
        trace_sink: TraceSink,
        id_factory: IdFactory | None = None,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._model = model
        self._validator = validator
        self._clock = clock
        self._trace = trace_sink
        self._id_factory = id_factory or _default_id_factory
        self._redactor = redactor or PersistenceRedactor()
        self._lineages: dict[str, ReplanContext] = {}

    def restore_trusted_lineage(self, context: ReplanContext) -> None:
        """Restore a checkpoint-verified cursor without invoking a model.

        The caller owns checkpoint schema/hash/run-identity verification. This
        narrow boundary is idempotent and refuses to overwrite a different
        live lineage for the same plan.
        """

        existing = self._lineages.get(context.prior_plan_id)
        if existing is not None and existing != context:
            raise ReplanLineageConflict(
                "restored replan lineage conflicts with memory"
            )
        self._lineages[context.prior_plan_id] = context.model_copy(deep=True)

    def plan(self, state: RunState, policy: PlanningPolicy) -> PlanningResult:
        self._require_planning(state)
        request = self._new_request(state, policy=policy)
        result = self._plan_once(state, request=request, prior_decision_id=None)
        self._lineages[result.plan_id] = result.replan_context
        return result

    def replan(
        self,
        state: RunState,
        *,
        prior_result: PlanningResult,
        request: ReplanRequest,
        planning_policy: PlanningPolicy,
        replan_policy: ReplanPolicy,
    ) -> ReplanResult:
        self._require_planning(state)
        requested_event = self._append_trace(
            state,
            TraceEventType.REPLAN_REQUESTED,
            correlation_id=request.request_id,
            attributes={
                "plan_id": request.context.prior_plan_id,
                "replan_count": request.context.replan_count,
                "reason_code": request.reason_code,
            },
        )
        context_valid = (
            request.run_id == state.run_id
            and prior_result.run_id == state.run_id
            and prior_result.run_revision == state.revision
            and prior_result.plan_id == request.context.prior_plan_id
            and prior_result.replan_context == request.context
            and self._lineages.get(request.context.prior_plan_id) == request.context
        )
        if not context_valid:
            return self._reject_replan(
                state,
                request,
                ReplanDecisionStatus.REJECTED_CONTEXT,
                causation_id=requested_event.event_id,
            )
        if request.context.replan_count >= replan_policy.max_replans:
            return self._reject_replan(
                state,
                request,
                ReplanDecisionStatus.REJECTED_LIMIT,
                causation_id=requested_event.event_id,
            )

        return self._approved_replan(
            state,
            request=request,
            planning_policy=planning_policy,
            causation_id=requested_event.event_id,
        )

    def replan_from_trusted_runtime_context(
        self,
        state: RunState,
        *,
        request: ReplanRequest,
        planning_policy: PlanningPolicy,
        replan_policy: ReplanPolicy,
    ) -> ReplanResult:
        """Consume a checkpoint-verified runtime lineage without lifecycle mutation."""

        if state.status is not RunStatus.RUNNING:
            raise PlanningPreconditionError(
                "trusted runtime replan requires RunStatus.RUNNING"
            )
        requested_event = self._append_trace(
            state,
            TraceEventType.REPLAN_REQUESTED,
            correlation_id=request.request_id,
            attributes={
                "plan_id": request.context.prior_plan_id,
                "replan_count": request.context.replan_count,
                "reason_code": request.reason_code,
                "runtime_replan": True,
            },
        )
        restored = self._lineages.get(request.context.prior_plan_id)
        if request.run_id != state.run_id or restored != request.context:
            return self._reject_replan(
                state,
                request,
                ReplanDecisionStatus.REJECTED_CONTEXT,
                causation_id=requested_event.event_id,
            )
        if request.context.replan_count >= replan_policy.max_replans:
            return self._reject_replan(
                state,
                request,
                ReplanDecisionStatus.REJECTED_LIMIT,
                causation_id=requested_event.event_id,
            )
        return self._approved_replan(
            state,
            request=request,
            planning_policy=planning_policy,
            causation_id=requested_event.event_id,
        )

    def _approved_replan(
        self,
        state: RunState,
        *,
        request: ReplanRequest,
        planning_policy: PlanningPolicy,
        causation_id: str,
    ) -> ReplanResult:
        decision = ReplanDecision(
            decision_id=self._new_id("decision"),
            request_id=request.request_id,
            run_id=state.run_id,
            prior_plan_id=request.context.prior_plan_id,
            prior_decision_id=request.context.prior_decision_id,
            current_replan_count=request.context.replan_count,
            next_replan_count=request.context.replan_count + 1,
            status=ReplanDecisionStatus.APPROVED,
            reason_code=request.reason_code,
        )
        decision_event = self._decision_trace(
            state, decision, causation_id=causation_id
        )
        # Consume the cursor before invoking the model so the same prior result
        # cannot fork an unbounded number of approved replans in this flow.
        del self._lineages[request.context.prior_plan_id]
        planning_request = self._new_request(
            state,
            policy=planning_policy,
            replan_count=decision.next_replan_count or 0,
            reason_code=request.reason_code,
            reason=request.reason,
        )
        result = self._plan_once(
            state,
            request=planning_request,
            prior_decision_id=decision.decision_id,
            causation_id=decision_event.event_id,
        )
        self._lineages[result.plan_id] = result.replan_context
        return ReplanResult(decision=decision, planning_result=result)

    def _plan_once(
        self,
        state: RunState,
        *,
        request: PlanningRequest,
        prior_decision_id: str | None,
        causation_id: str | None = None,
    ) -> PlanningResult:
        started = self._append_trace(
            state,
            TraceEventType.PLANNING_STARTED,
            correlation_id=request.request_id,
            causation_id=causation_id,
            attributes={
                "plan_id": request.plan_id,
                "run_revision": request.run_revision,
                "replan_count": request.replan_count,
                "reason_code": request.reason_code,
            },
        )
        context = ReplanContext(
            prior_plan_id=request.plan_id,
            replan_count=request.replan_count,
            prior_decision_id=prior_decision_id,
        )
        try:
            response = self._model.generate(request)
        except PlanningModelFailure as exc:
            error = PlanningError(
                code=exc.code,
                message=self._safe_text(str(exc)),
                retryable=exc.retryable,
            )
            self._append_trace(
                state,
                TraceEventType.PLANNING_MODEL_FAILED,
                correlation_id=request.request_id,
                causation_id=started.event_id,
                attributes={
                    "plan_id": request.plan_id,
                    "error_code": error.code,
                    "retryable": error.retryable,
                },
            )
            return PlanningResult(
                status=PlanningStatus.MODEL_ERROR,
                run_id=state.run_id,
                run_revision=request.run_revision,
                plan_id=request.plan_id,
                planning_request_id=request.request_id,
                validation=None,
                planning_error=error,
                replan_context=context,
            )

        candidate_received = self._append_trace(
            state,
            TraceEventType.PLANNING_CANDIDATE_RECEIVED,
            correlation_id=request.request_id,
            causation_id=started.event_id,
            attributes={
                "plan_id": request.plan_id,
                "planning_model_id": response.planning_model_id,
            },
        )
        try:
            candidate = CandidatePlan.model_validate(response.payload)
        except ValidationError as exc:
            validation = self._validator.malformed_result(
                self._candidate_validation_errors(exc)
            )
            self._validation_failed_trace(
                state,
                request,
                validation,
                causation_id=candidate_received.event_id,
            )
            return PlanningResult(
                status=PlanningStatus.MALFORMED,
                run_id=state.run_id,
                run_revision=request.run_revision,
                plan_id=request.plan_id,
                planning_request_id=request.request_id,
                validation=validation,
                replan_context=context,
            )

        validation = self._validator.validate(
            candidate, request, response.planning_model_id
        )
        if not validation.valid:
            self._validation_failed_trace(
                state,
                request,
                validation,
                causation_id=candidate_received.event_id,
            )
            return PlanningResult(
                status=PlanningStatus.INVALID,
                run_id=state.run_id,
                run_revision=request.run_revision,
                plan_id=request.plan_id,
                planning_request_id=request.request_id,
                validation=validation,
                replan_context=context,
            )

        dag = self._validator.build_dag(
            candidate,
            request,
            validation,
            dag_id=self._new_id("dag"),
            created_at=self._clock.now(),
        )
        self._append_trace(
            state,
            TraceEventType.PLANNING_VALIDATED,
            correlation_id=request.request_id,
            causation_id=candidate_received.event_id,
            attributes={
                "plan_id": request.plan_id,
                "dag_id": dag.dag_id,
                "task_count": validation.task_count,
                "graph_depth": validation.graph_depth,
            },
        )
        return PlanningResult(
            status=PlanningStatus.VALIDATED,
            run_id=state.run_id,
            run_revision=request.run_revision,
            plan_id=request.plan_id,
            planning_request_id=request.request_id,
            validation=validation,
            validated_dag=dag,
            replan_context=context,
        )

    def _new_request(
        self,
        state: RunState,
        *,
        policy: PlanningPolicy,
        replan_count: int = 0,
        reason_code: str | None = None,
        reason: str | None = None,
    ) -> PlanningRequest:
        limits = state.budget.limits
        usage = state.budget.usage
        return PlanningRequest(
            request_id=self._new_id("preq"),
            run_id=state.run_id,
            run_revision=state.revision,
            plan_id=self._new_id("plan"),
            query=state.input_snapshot.query,
            source_policy_id=state.config.source_policy_id,
            output_format=state.config.output_format,
            requested_at=self._clock.now(),
            allowed_capability_ids=state.config.allowed_capability_ids,
            remaining_budget=RemainingBudget(
                duration_milliseconds=(
                    limits.max_duration_seconds * 1_000 - usage.elapsed_milliseconds
                ),
                tokens=limits.max_tokens - usage.tokens,
                cost_microunits=(limits.max_cost_microunits - usage.cost_microunits),
                tool_calls=limits.max_tool_calls - usage.tool_calls,
            ),
            policy=policy,
            replan_count=replan_count,
            reason_code=reason_code,
            reason=reason,
        )

    def _reject_replan(
        self,
        state: RunState,
        request: ReplanRequest,
        status: ReplanDecisionStatus,
        *,
        causation_id: str,
    ) -> ReplanResult:
        decision = ReplanDecision(
            decision_id=self._new_id("decision"),
            request_id=request.request_id,
            run_id=state.run_id,
            prior_plan_id=request.context.prior_plan_id,
            prior_decision_id=request.context.prior_decision_id,
            current_replan_count=request.context.replan_count,
            status=status,
            reason_code=request.reason_code,
        )
        self._decision_trace(state, decision, causation_id=causation_id)
        return ReplanResult(decision=decision)

    def _decision_trace(
        self, state: RunState, decision: ReplanDecision, *, causation_id: str
    ) -> TraceEvent:
        return self._append_trace(
            state,
            TraceEventType.REPLAN_DECISION,
            correlation_id=decision.request_id,
            causation_id=causation_id,
            attributes={
                "decision_id": decision.decision_id,
                "plan_id": decision.prior_plan_id,
                "status": decision.status.value,
                "current_replan_count": decision.current_replan_count,
                "next_replan_count": decision.next_replan_count,
                "reason_code": decision.reason_code,
            },
        )

    def _validation_failed_trace(
        self,
        state: RunState,
        request: PlanningRequest,
        validation: ValidationResult,
        *,
        causation_id: str,
    ) -> None:
        self._append_trace(
            state,
            TraceEventType.PLANNING_VALIDATION_FAILED,
            correlation_id=request.request_id,
            causation_id=causation_id,
            attributes={
                "plan_id": request.plan_id,
                "issue_codes": [issue.code.value for issue in validation.issues],
                "issue_count": len(validation.issues),
            },
        )

    def _append_trace(
        self,
        state: RunState,
        event_type: TraceEventType,
        *,
        correlation_id: str,
        causation_id: str | None = None,
        attributes: dict[str, Any],
    ) -> TraceEvent:
        safe = self._redactor.redact_value(attributes)
        assert isinstance(safe, dict)
        event = TraceEvent(
            event_id=self._new_id("evt"),
            event_type=event_type,
            timestamp=self._clock.now(),
            run_id=state.run_id,
            revision=state.revision,
            previous_status=state.status,
            next_status=state.status,
            correlation_id=correlation_id,
            causation_id=causation_id,
            attributes=safe,
        )
        self._trace.append(event)
        return event

    def _safe_text(self, value: str) -> str:
        safe = self._redactor.redact_value(value)
        assert isinstance(safe, str)
        return safe[:1_000] or "planning model failed"

    @staticmethod
    def _candidate_validation_errors(
        exc: ValidationError,
    ) -> list[dict[str, object]]:
        sanitized: list[dict[str, object]] = []
        for error in exc.errors(include_input=False, include_url=False):
            location = [
                segment
                if isinstance(segment, int)
                or (
                    isinstance(segment, str)
                    and segment in _CANDIDATE_LOCATION_FIELDS
                )
                else "<field>"
                for segment in error.get("loc", ())
            ]
            error_type = error.get("type")
            stable_type = (
                error_type
                if isinstance(error_type, str)
                and error_type.replace("_", "").replace(".", "").isalnum()
                and len(error_type) <= 100
                else "validation_error"
            )
            sanitized.append(
                {
                    "loc": location,
                    "type": stable_type,
                    "message": "candidate field failed validation",
                }
            )
        return sanitized

    @staticmethod
    def _require_planning(state: RunState) -> None:
        if state.status is not RunStatus.PLANNING:
            raise PlanningPreconditionError(
                "PerspectivePlanner requires RunStatus.PLANNING"
            )

    def _new_id(self, prefix: str) -> str:
        return self._id_factory(prefix)
