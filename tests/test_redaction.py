import hashlib
from pathlib import Path

import pytest
from conftest import FrozenClock, SequentialIds

from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.adapters.memory import InMemoryTraceSink
from researchos.application.errors import UnsafePersistenceData
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import (
    RunConfig,
    RunInput,
    TraceEvent,
    TraceEventType,
    canonical_json_bytes,
)

SECRET = "Bearer abcdefghijklmnopqrstuvwxyz"


def test_hash_is_based_on_normalized_redacted_input(tmp_path: Path) -> None:
    clock = FrozenClock()
    manager = RunManager(
        store=FilesystemRunStore(tmp_path),
        trace_sink=FilesystemTraceSink(tmp_path),
        clock=clock,
        id_factory=SequentialIds(),
    )
    original_input = RunInput(query=f"  investigate {SECRET}\r\n")
    state = manager.create(original_input, RunConfig())

    assert state.input_redacted is True
    assert state.input_snapshot.query == "investigate [REDACTED]"
    expected_hash = hashlib.sha256(
        canonical_json_bytes(state.input_snapshot)
    ).hexdigest()
    original_hash = hashlib.sha256(canonical_json_bytes(original_input)).hexdigest()
    assert state.input_hash == expected_hash
    assert state.input_hash != original_hash
    reloaded = manager.load(state.run_id)
    assert reloaded.input_hash == state.input_hash
    all_bytes = b"".join(
        path.read_bytes() for path in (tmp_path / state.run_id).iterdir()
    )
    assert SECRET.encode() not in all_bytes


def test_adapter_rejects_unredacted_domain_object() -> None:
    trace = InMemoryTraceSink()
    unsafe = TraceEvent(
        event_id="evt_1",
        event_type=TraceEventType.RESUMED,
        timestamp=FrozenClock().now(),
        run_id="run_1",
        revision=0,
        attributes={"authorization": "secret"},
    )

    with pytest.raises(UnsafePersistenceData):
        trace.append(unsafe)
