from datetime import timedelta

import pytest

from researchos.application.errors import (
    InvalidTransition,
    RunConfigurationError,
    TerminalRunCannotResume,
)
from researchos.domain.contracts import (
    ErrorCategory,
    RunError,
    RunInput,
    RunStatus,
    TraceEventType,
)


def test_create_persists_effective_budget_and_trace(lifecycle: tuple) -> None:
    manager, store, trace, _, run_input, config = lifecycle

    state = manager.create(run_input, config)

    assert state.revision == 0
    assert state.status is RunStatus.CREATED
    assert state.budget.limits == config.budget_limits
    assert store.load(state.run_id) == state
    assert [event.event_type for event in trace.read(state.run_id)] == [
        TraceEventType.TRANSITION_INTENT,
        TraceEventType.TRANSITION_COMMITTED,
    ]


def test_load_is_strictly_read_only(lifecycle: tuple) -> None:
    manager, _, trace, _, run_input, config = lifecycle
    state = manager.create(run_input, config)
    before = trace.read(state.run_id)

    loaded = manager.load(state.run_id)

    assert loaded == state
    assert trace.read(state.run_id) == before


def test_strict_main_path_and_revision_monotonicity(lifecycle: tuple) -> None:
    manager, _, _, clock, run_input, config = lifecycle
    state = manager.create(run_input, config)
    for revision, status in enumerate(
        [
            RunStatus.PLANNING,
            RunStatus.READY,
            RunStatus.RUNNING,
            RunStatus.VERIFYING,
            RunStatus.EVALUATING,
        ],
        start=1,
    ):
        clock.advance(seconds=1)
        state = manager.transition(state.run_id, status)
        assert state.revision == revision
    state = manager.finalize(state.run_id, RunStatus.COMPLETED)
    assert state.revision == 6
    assert state.completed_at is not None


def test_failed_finalization_requires_structured_error(lifecycle: tuple) -> None:
    manager, _, _, clock, run_input, config = lifecycle
    state = manager.create(run_input, config)

    with pytest.raises(ValueError, match="failed state requires"):
        manager.finalize(state.run_id, RunStatus.FAILED, reason="failure")

    error = RunError(
        error_id="error_1",
        code="storage_failure",
        category=ErrorCategory.STORAGE,
        message="safe failure",
        occurred_at=clock.now(),
    )
    failed = manager.finalize(
        state.run_id,
        RunStatus.FAILED,
        reason="failure",
        errors=[error],
    )
    assert failed.status is RunStatus.FAILED


def test_resume_validates_hash_and_records_resumed(lifecycle: tuple) -> None:
    manager, _, trace, _, run_input, config = lifecycle
    state = manager.create(run_input, config)

    resumed = manager.resume(
        state.run_id,
        expected_input=RunInput(query="  test query\r\n"),
        expected_config=config,
    )

    assert resumed == state
    assert trace.read(state.run_id)[-1].event_type is TraceEventType.RESUMED
    with pytest.raises(RunConfigurationError, match="input hash"):
        manager.resume(
            state.run_id,
            expected_input=RunInput(query="different"),
            expected_config=config,
        )


def test_terminal_run_cannot_resume(lifecycle: tuple) -> None:
    manager, _, _, _, run_input, config = lifecycle
    state = manager.create(run_input, config)
    state = manager.finalize(
        state.run_id,
        RunStatus.CANCELLED,
        reason="cancelled",
    )

    with pytest.raises(TerminalRunCannotResume):
        manager.resume(
            state.run_id,
            expected_input=run_input,
            expected_config=config,
        )


def test_invalid_skip_does_not_change_revision(lifecycle: tuple) -> None:
    manager, store, _, _, run_input, config = lifecycle
    state = manager.create(run_input, config)

    with pytest.raises(InvalidTransition):
        manager.transition(state.run_id, RunStatus.RUNNING)
    assert store.load(state.run_id).revision == 0


def test_deadline_semantics_use_injected_clock(lifecycle: tuple) -> None:
    manager, _, _, clock, run_input, config = lifecycle
    expired = config.model_copy(update={"deadline": clock.now() - timedelta(seconds=1)})

    with pytest.raises(RunConfigurationError, match="deadline"):
        manager.create(run_input, expired)
