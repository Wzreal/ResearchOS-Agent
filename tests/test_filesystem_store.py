import json
from pathlib import Path

import pytest
from conftest import FrozenClock, SequentialIds

from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.application.errors import CorruptRunState, StatePersistenceError
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import (
    RunConfig,
    RunInput,
    RunState,
    RunStatus,
    canonical_json_bytes,
)


class FailOnce:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.failed = False

    def __call__(self, stage: str) -> None:
        if stage == self.stage and not self.failed:
            self.failed = True
            raise OSError(f"injected {stage}")


def create_filesystem_run(tmp_path: Path) -> tuple[RunManager, RunState]:
    manager = RunManager(
        store=FilesystemRunStore(tmp_path),
        trace_sink=FilesystemTraceSink(tmp_path),
        clock=FrozenClock(),
        id_factory=SequentialIds(),
    )
    state = manager.create(RunInput(query="query"), RunConfig())
    return manager, state


def test_filesystem_round_trip_across_instances(tmp_path: Path) -> None:
    manager, state = create_filesystem_run(tmp_path)

    assert manager.load(state.run_id) == FilesystemRunStore(tmp_path).load(state.run_id)


@pytest.mark.parametrize(
    "stage",
    [
        "before_temp_write",
        "after_temp_write",
        "after_temp_flush",
        "after_temp_fsync",
        "before_replace",
    ],
)
def test_failure_before_replace_preserves_old_state(
    tmp_path: Path, stage: str
) -> None:
    _, state = create_filesystem_run(tmp_path)
    failing_store = FilesystemRunStore(tmp_path, fault_injector=FailOnce(stage))
    raw = state.model_dump(mode="python")
    raw.update(
        revision=1,
        status=RunStatus.PLANNING,
        last_transition_id="tr_manual",
    )
    changed = RunState.model_validate(raw)

    with pytest.raises(StatePersistenceError) as captured:
        failing_store.save(changed, expected_revision=0)

    assert captured.value.state_replaced is False
    assert captured.value.run_id == state.run_id
    assert FilesystemRunStore(tmp_path).load(state.run_id) == state
    assert not list((tmp_path / state.run_id).glob("*.tmp"))


def test_failure_after_replace_reports_state_replaced(tmp_path: Path) -> None:
    _, state = create_filesystem_run(tmp_path)
    failing_store = FilesystemRunStore(
        tmp_path, fault_injector=FailOnce("after_replace")
    )
    raw = state.model_dump(mode="python")
    raw.update(
        revision=1,
        status=RunStatus.PLANNING,
        last_transition_id="tr_manual",
    )
    changed = RunState.model_validate(raw)

    with pytest.raises(StatePersistenceError) as captured:
        failing_store.save(changed, expected_revision=0)

    assert captured.value.state_replaced is True
    assert captured.value.run_id == state.run_id
    assert FilesystemRunStore(tmp_path).load(state.run_id).revision == 1


def test_middle_trace_corruption_is_not_repaired(tmp_path: Path) -> None:
    _, state = create_filesystem_run(tmp_path)
    trace_path = tmp_path / state.run_id / "trace.jsonl"
    original = trace_path.read_bytes()
    first_newline = original.index(b"\n") + 1
    corrupted = original[:first_newline] + b"not-json\n" + original[first_newline:]
    trace_path.write_bytes(corrupted)

    with pytest.raises(CorruptRunState, match="trace line"):
        FilesystemTraceSink(tmp_path).read(
            state.run_id, recover_torn_tail=True
        )
    assert b"not-json\n" in trace_path.read_bytes()


def test_state_hash_corruption_is_rejected(tmp_path: Path) -> None:
    _, state = create_filesystem_run(tmp_path)
    state_path = tmp_path / state.run_id / "run_state.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["input_hash"] = "0" * 64
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CorruptRunState, match="input hash"):
        FilesystemRunStore(tmp_path).load(state.run_id)


def test_non_object_state_and_trace_are_corruption(tmp_path: Path) -> None:
    _, state = create_filesystem_run(tmp_path)
    run_dir = tmp_path / state.run_id
    (run_dir / "run_state.json").write_text("[]", encoding="utf-8")
    with pytest.raises(CorruptRunState, match="JSON object"):
        FilesystemRunStore(tmp_path).load(state.run_id)

    (run_dir / "trace.jsonl").write_text("[]\n", encoding="utf-8")
    with pytest.raises(CorruptRunState, match="JSON object"):
        FilesystemTraceSink(tmp_path).read(state.run_id)


def test_trace_rejects_event_from_another_run(tmp_path: Path) -> None:
    manager, first = create_filesystem_run(tmp_path)
    second = manager.create(RunInput(query="second query"), RunConfig())
    foreign_event = FilesystemTraceSink(tmp_path).read(second.run_id)[0]
    first_trace = tmp_path / first.run_id / "trace.jsonl"
    with first_trace.open("ab") as handle:
        handle.write(canonical_json_bytes(foreign_event) + b"\n")

    with pytest.raises(CorruptRunState, match="belongs to run"):
        FilesystemTraceSink(tmp_path).read(first.run_id)


def test_trace_load_rejects_event_requiring_redaction(tmp_path: Path) -> None:
    _, state = create_filesystem_run(tmp_path)
    trace_path = tmp_path / state.run_id / "trace.jsonl"
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    unsafe_event = json.loads(lines[0])
    unsafe_event["attributes"] = {"authorization": "Bearer secret-value"}
    lines[0] = json.dumps(unsafe_event, separators=(",", ":"), sort_keys=True)
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(CorruptRunState, match="unsafe persisted data"):
        FilesystemTraceSink(tmp_path).read(state.run_id)
