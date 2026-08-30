"""Phase 1 run lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any
from uuid import uuid4

from researchos.application.errors import (
    CorruptRunState,
    InvalidTransition,
    RunConfigurationError,
    TerminalRunCannotResume,
    TraceCommitError,
)
from researchos.domain.contracts import (
    TERMINAL_STATUSES,
    ArtifactRecord,
    Budget,
    RunConfig,
    RunError,
    RunInput,
    RunState,
    RunStatus,
    TraceEvent,
    TraceEventType,
    model_sha256,
)
from researchos.interfaces.lifecycle import Clock, RunStore, TraceSink
from researchos.security.redaction import (
    PersistenceRedactor,
    prepare_persistent_run_input,
)

IdFactory = Callable[[str], str]

LEGAL_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset(
        {RunStatus.PLANNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.PLANNING: frozenset(
        {RunStatus.READY, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.READY: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.VERIFYING,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.VERIFYING: frozenset(
        {
            RunStatus.EVALUATING,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.EVALUATING: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.PARTIAL: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class RunManager:
    """Own creation, transitions, finalization, load, and lifecycle resume."""

    def __init__(
        self,
        *,
        store: RunStore,
        clock: Clock,
        trace_sink: TraceSink,
        id_factory: IdFactory | None = None,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._trace = trace_sink
        self._id_factory = id_factory or _default_id_factory
        self._redactor = redactor or PersistenceRedactor()

    def create(self, run_input: RunInput, config: RunConfig) -> RunState:
        now = self._clock.now()
        self._validate_config(config, now=now)
        persistent_input, input_redacted = self._prepare_input(run_input)
        run_id = self._new_id("run")
        transition_id = self._new_id("tr")
        state = RunState(
            run_id=run_id,
            revision=0,
            status=RunStatus.CREATED,
            input_snapshot=persistent_input,
            input_hash=self._hash_model(persistent_input),
            input_redacted=input_redacted,
            config=config,
            config_hash=self._hash_model(config),
            created_at=now,
            updated_at=now,
            budget=Budget(limits=config.budget_limits),
            last_transition_id=transition_id,
        )
        intent = self._transition_event(
            event_type=TraceEventType.TRANSITION_INTENT,
            state=state,
            transition_id=transition_id,
            previous_status=None,
            next_status=RunStatus.CREATED,
        )
        self._trace.append(intent)
        self._store.create(state)
        self._append_committed(
            state,
            transition_id=transition_id,
            previous_status=None,
            next_status=RunStatus.CREATED,
            causation_id=intent.event_id,
        )
        return state

    def load(self, run_id: str) -> RunState:
        """Load state without writing state or trace."""

        return self._store.load(run_id)

    def transition(self, run_id: str, next_status: RunStatus) -> RunState:
        if next_status in TERMINAL_STATUSES:
            raise InvalidTransition("terminal states must be entered through finalize")
        current = self._load_for_mutation(run_id)
        self._require_transition(current.status, next_status)
        return self._commit_state_change(current, next_status=next_status)

    def finalize(
        self,
        run_id: str,
        terminal_status: RunStatus,
        *,
        reason: str | None = None,
        errors: Iterable[RunError] = (),
        artifacts: Iterable[ArtifactRecord] = (),
    ) -> RunState:
        if terminal_status not in TERMINAL_STATUSES:
            raise InvalidTransition("finalize requires a terminal status")
        current = self._load_for_mutation(run_id)
        self._require_transition(current.status, terminal_status)
        safe_reason = (
            self._redactor.redact_value(reason) if reason is not None else None
        )
        assert safe_reason is None or isinstance(safe_reason, str)
        safe_errors = tuple(self._sanitize_error(error) for error in errors)
        return self._commit_state_change(
            current,
            next_status=terminal_status,
            terminal_reason=safe_reason,
            errors=current.errors + safe_errors,
            artifacts=current.artifacts + tuple(artifacts),
        )

    def resume(
        self,
        run_id: str,
        *,
        expected_input: RunInput,
        expected_config: RunConfig,
    ) -> RunState:
        now = self._clock.now()
        self._validate_config(expected_config, now=now)
        persistent_input, _ = self._prepare_input(expected_input)
        state = self._store.load(run_id)
        if state.input_hash != self._hash_model(persistent_input):
            raise RunConfigurationError(
                "resume input hash does not match persisted run"
            )
        if state.config_hash != self._hash_model(expected_config):
            raise RunConfigurationError("resume configuration hash does not match")
        if state.status in TERMINAL_STATUSES:
            raise TerminalRunCannotResume(f"run is already {state.status.value}")
        self._reconcile(state)
        resumed = TraceEvent(
            event_id=self._new_id("evt"),
            event_type=TraceEventType.RESUMED,
            timestamp=now,
            run_id=state.run_id,
            revision=state.revision,
            previous_status=state.status,
            next_status=state.status,
            attributes={"mode": state.config.mode.value},
        )
        self._trace.append(resumed)
        return state

    def _load_for_mutation(self, run_id: str) -> RunState:
        state = self._store.load(run_id)
        self._reconcile(state)
        return state

    def _commit_state_change(
        self,
        current: RunState,
        *,
        next_status: RunStatus,
        terminal_reason: str | None = None,
        errors: tuple[RunError, ...] | None = None,
        artifacts: tuple[ArtifactRecord, ...] | None = None,
    ) -> RunState:
        target_revision = current.revision + 1
        transition_id = self._new_id("tr")
        now = self._clock.now()
        raw: dict[str, Any] = current.model_dump(mode="python")
        raw.update(
            revision=target_revision,
            status=next_status,
            updated_at=now,
            completed_at=now if next_status in TERMINAL_STATUSES else None,
            terminal_reason=terminal_reason,
            last_transition_id=transition_id,
        )
        if errors is not None:
            raw["errors"] = errors
        if artifacts is not None:
            raw["artifacts"] = artifacts
        new_state = RunState.model_validate(raw)
        intent = self._transition_event(
            event_type=TraceEventType.TRANSITION_INTENT,
            state=new_state,
            transition_id=transition_id,
            previous_status=current.status,
            next_status=next_status,
        )
        self._trace.append(intent)
        self._store.save(new_state, expected_revision=current.revision)
        self._append_committed(
            new_state,
            transition_id=transition_id,
            previous_status=current.status,
            next_status=next_status,
            causation_id=intent.event_id,
        )
        return new_state

    def _append_committed(
        self,
        state: RunState,
        *,
        transition_id: str,
        previous_status: RunStatus | None,
        next_status: RunStatus,
        causation_id: str,
    ) -> None:
        committed = self._transition_event(
            event_type=TraceEventType.TRANSITION_COMMITTED,
            state=state,
            transition_id=transition_id,
            previous_status=previous_status,
            next_status=next_status,
            causation_id=causation_id,
        )
        try:
            self._trace.append(committed)
        except Exception as exc:
            raise TraceCommitError(
                "state committed but transition_committed trace was not confirmed",
                run_id=state.run_id,
                transition_id=transition_id,
                revision=state.revision,
            ) from exc

    def _reconcile(self, state: RunState) -> None:
        events = self._trace.read(state.run_id, recover_torn_tail=True)
        event_ids = [event.event_id for event in events]
        if len(event_ids) != len(set(event_ids)):
            raise CorruptRunState("trace contains duplicate event IDs")

        settled_types = {
            TraceEventType.TRANSITION_COMMITTED,
            TraceEventType.TRANSITION_RECONCILED,
        }
        if any(
            event.event_type in settled_types and event.revision > state.revision
            for event in events
        ):
            raise CorruptRunState("trace commits a revision newer than run state")

        current_commits = [
            event
            for event in events
            if event.revision == state.revision
            and event.event_type is TraceEventType.TRANSITION_COMMITTED
        ]
        current_reconciled = [
            event
            for event in events
            if event.revision == state.revision
            and event.event_type is TraceEventType.TRANSITION_RECONCILED
        ]
        for event in (*current_commits, *current_reconciled):
            if event.transition_id != state.last_transition_id:
                raise CorruptRunState("state and settled trace transition IDs differ")
        if current_commits and current_reconciled:
            raise CorruptRunState("revision has both committed and reconciled events")
        if len(current_commits) > 1 or len(current_reconciled) > 1:
            raise CorruptRunState("revision has duplicate settlement events")
        intents = [
            event
            for event in events
            if event.revision == state.revision
            and event.event_type is TraceEventType.TRANSITION_INTENT
            and event.transition_id == state.last_transition_id
        ]
        if len(intents) != 1:
            raise CorruptRunState(
                "committed state lacks one matching transition intent"
            )
        intent = intents[0]
        if intent.next_status is not state.status:
            raise CorruptRunState("transition intent target differs from run state")
        settled = current_commits or current_reconciled
        if settled:
            event = settled[0]
            if (
                event.previous_status is not intent.previous_status
                or event.next_status is not intent.next_status
            ):
                raise CorruptRunState(
                    "settled transition fields differ from transition intent"
                )
            return
        recovery_event_id = f"evt_reconcile_{state.last_transition_id}"
        if any(event.event_id == recovery_event_id for event in events):
            return
        reconciled = self._transition_event(
            event_type=TraceEventType.TRANSITION_RECONCILED,
            state=state,
            transition_id=state.last_transition_id,
            previous_status=intent.previous_status,
            next_status=state.status,
            event_id=recovery_event_id,
            causation_id=intent.event_id,
            attributes={"reason": "state_committed_trace_commit_missing"},
        )
        self._trace.append(reconciled)

    def _transition_event(
        self,
        *,
        event_type: TraceEventType,
        state: RunState,
        transition_id: str,
        previous_status: RunStatus | None,
        next_status: RunStatus,
        event_id: str | None = None,
        causation_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> TraceEvent:
        safe_attributes = self._redactor.redact_value(attributes or {})
        assert isinstance(safe_attributes, dict)
        return TraceEvent(
            event_id=event_id or self._new_id("evt"),
            event_type=event_type,
            timestamp=self._clock.now(),
            run_id=state.run_id,
            transition_id=transition_id,
            revision=state.revision,
            previous_status=previous_status,
            next_status=next_status,
            correlation_id=transition_id,
            causation_id=causation_id,
            attributes=safe_attributes,
        )

    def _prepare_input(self, run_input: RunInput) -> tuple[RunInput, bool]:
        return prepare_persistent_run_input(run_input)

    def _sanitize_error(self, error: RunError) -> RunError:
        safe = self._redactor.redact_value(error.model_dump(mode="python"))
        assert isinstance(safe, dict)
        return RunError.model_validate(safe)

    @staticmethod
    def _hash_model(model: RunInput | RunConfig) -> str:
        return model_sha256(model)

    @staticmethod
    def _validate_config(config: RunConfig, *, now: datetime) -> None:
        if config.mode.value == "real":
            raise RunConfigurationError(
                "real mode has no configured real adapters in Phase 1"
            )
        if config.deadline is not None and config.deadline <= now:
            raise RunConfigurationError("deadline must be later than the current time")

    @staticmethod
    def _require_transition(current: RunStatus, target: RunStatus) -> None:
        if target not in LEGAL_TRANSITIONS[current]:
            raise InvalidTransition(f"cannot transition from {current} to {target}")

    def _new_id(self, prefix: str) -> str:
        return self._id_factory(prefix)
