from pathlib import Path

import pytest
from conftest import FrozenClock, SequentialIds

from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.application.errors import (
    CorruptRunState,
    StatePersistenceError,
    TraceCommitError,
)
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import (
    RunConfig,
    RunInput,
    RunStatus,
    TraceEvent,
    TraceEventType,
)


class FailOccurrence:
    def __init__(self, stage: str, occurrence: int) -> None:
        self.stage = stage
        self.occurrence = occurrence
        self.seen = 0

    def __call__(self, stage: str) -> None:
        if stage == self.stage:
            self.seen += 1
            if self.seen == self.occurrence:
                raise OSError(f"injected {stage} occurrence {self.occurrence}")


def manager_for(
    root: Path,
    *,
    store_fault: FailOccurrence | None = None,
    trace_fault: FailOccurrence | None = None,
) -> RunManager:
    return RunManager(
        store=FilesystemRunStore(root, fault_injector=store_fault),
        trace_sink=FilesystemTraceSink(root, fault_injector=trace_fault),
        clock=FrozenClock(),
        id_factory=SequentialIds(),
    )


def test_intent_without_state_commit_is_ignored(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    run_input = RunInput(query="query")
    config = RunConfig()
    state = manager.create(run_input, config)
    failing = manager_for(
        tmp_path,
        store_fault=FailOccurrence("before_replace", 1),
    )

    with pytest.raises(StatePersistenceError):
        failing.transition(state.run_id, RunStatus.PLANNING)

    resumed = manager_for(tmp_path).resume(
        state.run_id,
        expected_input=run_input,
        expected_config=config,
    )
    events = FilesystemTraceSink(tmp_path).read(state.run_id)
    assert resumed.revision == 0
    assert not any(
        event.event_type is TraceEventType.TRANSITION_RECONCILED
        and event.revision == 1
        for event in events
    )


def test_state_without_commit_is_reconciled_once(tmp_path: Path) -> None:
    run_input = RunInput(query="query")
    config = RunConfig()
    manager = manager_for(tmp_path)
    state = manager.create(run_input, config)
    failing = manager_for(
        tmp_path,
        trace_fault=FailOccurrence("before_trace_append", 2),
    )

    with pytest.raises(TraceCommitError) as captured:
        failing.transition(state.run_id, RunStatus.PLANNING)
    assert captured.value.state_committed is True

    recovery = manager_for(tmp_path)
    recovery.resume(
        state.run_id,
        expected_input=run_input,
        expected_config=config,
    )
    recovery.resume(
        state.run_id,
        expected_input=run_input,
        expected_config=config,
    )
    reconciled = [
        event
        for event in FilesystemTraceSink(tmp_path).read(state.run_id)
        if event.event_type is TraceEventType.TRANSITION_RECONCILED
    ]
    assert len(reconciled) == 1
    assert reconciled[0].attributes == {
        "reason": "state_committed_trace_commit_missing"
    }


def test_commit_newer_than_state_is_corruption(tmp_path: Path) -> None:
    run_input = RunInput(query="query")
    config = RunConfig()
    manager = manager_for(tmp_path)
    state = manager.create(run_input, config)
    trace = FilesystemTraceSink(tmp_path)
    transition_id = "tr_future"
    for event_type in (
        TraceEventType.TRANSITION_INTENT,
        TraceEventType.TRANSITION_COMMITTED,
    ):
        trace.append(
            TraceEvent(
                event_id=f"evt_{event_type.name.lower()}",
                event_type=event_type,
                timestamp=FrozenClock().now(),
                run_id=state.run_id,
                transition_id=transition_id,
                revision=1,
                previous_status=RunStatus.CREATED,
                next_status=RunStatus.PLANNING,
            )
        )

    with pytest.raises(CorruptRunState, match="newer than run state"):
        manager_for(tmp_path).resume(
            state.run_id,
            expected_input=run_input,
            expected_config=config,
        )


def test_committed_state_without_matching_intent_is_corruption(
    tmp_path: Path,
) -> None:
    run_input = RunInput(query="query")
    config = RunConfig()
    manager = manager_for(tmp_path)
    state = manager.create(run_input, config)
    trace_path = tmp_path / state.run_id / "trace.jsonl"
    lines = trace_path.read_bytes().splitlines(keepends=True)
    trace_path.write_bytes(b"".join(lines[1:]))

    with pytest.raises(CorruptRunState, match="matching transition intent"):
        manager_for(tmp_path).resume(
            state.run_id,
            expected_input=run_input,
            expected_config=config,
        )


def test_load_does_not_repair_torn_tail_but_resume_does(tmp_path: Path) -> None:
    run_input = RunInput(query="query")
    config = RunConfig()
    manager = manager_for(tmp_path)
    state = manager.create(run_input, config)
    trace_path = tmp_path / state.run_id / "trace.jsonl"
    trace_path.write_bytes(trace_path.read_bytes() + b'{"torn":')
    torn = trace_path.read_bytes()

    assert manager.load(state.run_id) == state
    assert trace_path.read_bytes() == torn

    manager_for(tmp_path).resume(
        state.run_id,
        expected_input=run_input,
        expected_config=config,
    )
    repaired = trace_path.read_bytes()
    assert repaired.endswith(b"\n")
    assert b'{"torn":' not in repaired
    assert any(
        event.event_type is TraceEventType.RESUMED
        for event in FilesystemTraceSink(tmp_path).read(state.run_id)
    )


def test_torn_tail_failure_before_truncate_preserves_tail(tmp_path: Path) -> None:
    run_input = RunInput(query="query")
    config = RunConfig()
    manager = manager_for(tmp_path)
    state = manager.create(run_input, config)
    trace_path = tmp_path / state.run_id / "trace.jsonl"
    trace_path.write_bytes(trace_path.read_bytes() + b'{"torn":')
    torn = trace_path.read_bytes()
    failing = manager_for(
        tmp_path,
        trace_fault=FailOccurrence("before_trace_tail_truncate", 1),
    )

    with pytest.raises(OSError, match="before_trace_tail_truncate"):
        failing.resume(
            state.run_id,
            expected_input=run_input,
            expected_config=config,
        )
    assert trace_path.read_bytes() == torn


def test_torn_tail_failure_after_fsync_is_idempotent(tmp_path: Path) -> None:
    run_input = RunInput(query="query")
    config = RunConfig()
    manager = manager_for(tmp_path)
    state = manager.create(run_input, config)
    trace_path = tmp_path / state.run_id / "trace.jsonl"
    trace_path.write_bytes(trace_path.read_bytes() + b'{"torn":')
    failing = manager_for(
        tmp_path,
        trace_fault=FailOccurrence("after_trace_tail_fsync", 1),
    )

    with pytest.raises(OSError, match="after_trace_tail_fsync"):
        failing.resume(
            state.run_id,
            expected_input=run_input,
            expected_config=config,
        )
    assert trace_path.read_bytes().endswith(b"\n")

    resumed = manager_for(tmp_path).resume(
        state.run_id,
        expected_input=run_input,
        expected_config=config,
    )
    assert resumed == state
