from __future__ import annotations

from typing import Any

import pytest
from runtime_fixtures import RuntimeIds, build_executor, runtime_example

from researchos.adapters.checkpoint_filesystem import FilesystemCheckpointStore
from researchos.adapters.checkpoint_memory import InMemoryCheckpointStore
from researchos.adapters.mock_execution import MockTaskExecutionBackend
from researchos.application.checkpoint_manager import CheckpointManager
from researchos.application.errors import (
    CheckpointPersistenceError,
    CheckpointRevisionConflict,
    CorruptCheckpoint,
    RuntimeTraceCommitError,
    UnsafePersistenceData,
)
from researchos.domain.contracts import TraceEvent, TraceEventType, model_sha256
from researchos.domain.runtime import (
    CheckpointMutation,
    RuntimeCheckpoint,
)


class FailOnceTrace:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.fail_type: TraceEventType | None = None
        self.failed = False

    def append(self, event: TraceEvent) -> None:
        if event.event_type is self.fail_type and not self.failed:
            self.failed = True
            raise OSError("injected trace failure")
        self.delegate.append(event)

    def read(self, run_id: str, *, recover_torn_tail: bool = False):
        return self.delegate.read(run_id, recover_torn_tail=recover_torn_tail)


def test_outbox_replays_exact_original_descriptor_after_crash() -> None:
    example = runtime_example()
    store = InMemoryCheckpointStore()
    trace = FailOnceTrace(example.trace)
    ids = RuntimeIds()
    manager = CheckpointManager(
        store=store, trace_sink=trace, clock=example.clock, id_factory=ids
    )
    executor, _, _, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    # Use the manager under test while keeping construction logic identical.
    executor._checkpoints = manager
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    original = TraceEvent(
        event_id="evt_original",
        event_type=TraceEventType.TASK_READY,
        timestamp=example.clock.now(),
        run_id=example.state.run_id,
        revision=example.state.revision,
        correlation_id="mutation_replay",
        attributes={"task_id": "task_a"},
    )
    descriptor = manager.descriptor(original)
    target = RuntimeCheckpoint.model_validate(
        checkpoint.model_copy(
            update={
                "checkpoint_revision": 1,
                "last_mutation": CheckpointMutation(
                    mutation_id="mutation_replay",
                    kind="ready",
                    events=(descriptor,),
                ),
            }
        ).model_dump(mode="python")
    )
    trace.fail_type = TraceEventType.TASK_READY

    with pytest.raises(RuntimeTraceCommitError) as captured:
        manager.commit(target, expected_revision=0)
    assert captured.value.checkpoint_committed

    trace.fail_type = None
    recovered = manager.load_and_reconcile(example.state.run_id)
    replayed = next(
        event
        for event in example.trace.read(example.state.run_id)
        if event.event_id == original.event_id
    )
    assert recovered == target
    assert replayed == original
    assert any(
        event.event_type is TraceEventType.CHECKPOINT_RECONCILED
        for event in example.trace.read(example.state.run_id)
    )


def test_recovery_is_idempotent() -> None:
    example = runtime_example()
    backend = MockTaskExecutionBackend({})
    executor, manager, _, _, _ = build_executor(example, backend)
    executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    manager.load_and_reconcile(example.state.run_id)
    first = example.trace.read(example.state.run_id)
    manager.load_and_reconcile(example.state.run_id)
    assert example.trace.read(example.state.run_id) == first


def test_partial_outbox_replays_only_missing_original_event() -> None:
    example = runtime_example()
    store = InMemoryCheckpointStore()
    trace = FailOnceTrace(example.trace)
    manager = CheckpointManager(
        store=store,
        trace_sink=trace,
        clock=example.clock,
        id_factory=RuntimeIds(),
    )
    executor, _, _, _, _ = build_executor(
        example, MockTaskExecutionBackend({})
    )
    executor._checkpoints = manager
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    first = TraceEvent(
        event_id="evt_partial_first",
        event_type=TraceEventType.TASK_READY,
        timestamp=example.clock.now(),
        run_id=example.state.run_id,
        revision=example.state.revision,
        correlation_id="mutation_partial",
        attributes={"task_id": "task_a"},
    )
    second = TraceEvent(
        event_id="evt_partial_second",
        event_type=TraceEventType.TASK_BLOCKED,
        timestamp=example.clock.now(),
        run_id=example.state.run_id,
        revision=example.state.revision,
        correlation_id="mutation_partial",
        attributes={"task_id": "task_b", "blocked_by": ["task_a"]},
    )
    target = RuntimeCheckpoint.model_validate(
        checkpoint.model_copy(
            update={
                "checkpoint_revision": 1,
                "last_mutation": CheckpointMutation(
                    mutation_id="mutation_partial",
                    kind="partial",
                    events=(manager.descriptor(first), manager.descriptor(second)),
                ),
            }
        ).model_dump(mode="python")
    )
    trace.fail_type = TraceEventType.TASK_BLOCKED
    with pytest.raises(RuntimeTraceCommitError):
        manager.commit(target, expected_revision=0)

    trace.fail_type = None
    manager.load_and_reconcile(example.state.run_id)
    events = example.trace.read(example.state.run_id)
    assert sum(event.event_id == first.event_id for event in events) == 1
    assert sum(event.event_id == second.event_id for event in events) == 1


def test_missing_committed_append_recovers_as_reconciled_not_committed() -> None:
    example = runtime_example()
    store = InMemoryCheckpointStore()
    trace = FailOnceTrace(example.trace)
    manager = CheckpointManager(
        store=store,
        trace_sink=trace,
        clock=example.clock,
        id_factory=RuntimeIds(),
    )
    executor, _, _, _, _ = build_executor(
        example, MockTaskExecutionBackend({})
    )
    executor._checkpoints = manager
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    target = checkpoint.model_copy(
        update={
            "checkpoint_revision": 1,
            "last_mutation": CheckpointMutation(
                mutation_id="mutation_commit_failure",
                kind="empty_settlement",
                events=(),
            ),
        }
    )
    trace.fail_type = TraceEventType.CHECKPOINT_COMMITTED
    with pytest.raises(RuntimeTraceCommitError):
        manager.commit(target, expected_revision=0)
    trace.fail_type = None

    manager.load_and_reconcile(example.state.run_id)
    revision_events = [
        event
        for event in example.trace.read(example.state.run_id)
        if event.attributes.get("checkpoint_revision") == 1
    ]
    assert not any(
        event.event_type is TraceEventType.CHECKPOINT_COMMITTED
        for event in revision_events
    )
    assert any(
        event.event_type is TraceEventType.CHECKPOINT_RECONCILED
        for event in revision_events
    )


def test_trace_settlement_newer_than_checkpoint_is_corruption() -> None:
    example = runtime_example()
    executor, manager, _, _, _ = build_executor(
        example, MockTaskExecutionBackend({})
    )
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    example.trace.append(
        TraceEvent(
            event_id="evt_future_commit",
            event_type=TraceEventType.CHECKPOINT_COMMITTED,
            timestamp=example.clock.now(),
            run_id=example.state.run_id,
            revision=example.state.revision,
            correlation_id="mutation_future",
            attributes={
                "checkpoint_revision": checkpoint.checkpoint_revision + 1,
                "mutation_id": "mutation_future",
                "mutation_kind": "future",
            },
        )
    )
    with pytest.raises(CorruptCheckpoint, match="newer"):
        manager.load_and_reconcile(example.state.run_id)


def test_checkpoint_without_intent_is_corruption() -> None:
    example = runtime_example()
    backend = MockTaskExecutionBackend({})
    executor, _, _, _, _ = build_executor(example, backend)
    # Create through a separate manager, then copy only the checkpoint.
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    isolated_store = InMemoryCheckpointStore()
    isolated_store.create(checkpoint)
    from researchos.adapters.memory import InMemoryTraceSink

    manager = CheckpointManager(
        store=isolated_store,
        trace_sink=InMemoryTraceSink(),
        clock=example.clock,
        id_factory=RuntimeIds(),
    )
    with pytest.raises(CorruptCheckpoint, match="intent"):
        manager.load_and_reconcile(example.state.run_id)


def test_filesystem_checkpoint_reports_replace_uncertainty(tmp_path) -> None:
    example = runtime_example()
    backend = MockTaskExecutionBackend({})
    executor, _, _, _, _ = build_executor(example, backend)
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    fail = {"enabled": False}

    def injector(stage: str) -> None:
        if fail["enabled"] and stage == "after_replace":
            raise OSError("injected")

    store = FilesystemCheckpointStore(tmp_path, fault_injector=injector)
    store.create(checkpoint)
    target = checkpoint.model_copy(
        update={"checkpoint_revision": 1, "updated_at": example.clock.now()}
    )
    fail["enabled"] = True
    with pytest.raises(CheckpointPersistenceError) as captured:
        store.save(target, expected_revision=0)
    assert captured.value.run_id == checkpoint.run_id
    assert captured.value.checkpoint_revision == 1
    assert captured.value.checkpoint_replaced


def test_intent_with_failed_snapshot_replace_leaves_old_checkpoint(tmp_path) -> None:
    example = runtime_example()
    fail = {"enabled": False}

    def injector(stage: str) -> None:
        if fail["enabled"] and stage == "before_replace":
            raise OSError("injected")

    store = FilesystemCheckpointStore(tmp_path, fault_injector=injector)
    ids = RuntimeIds()
    manager = CheckpointManager(
        store=store,
        trace_sink=example.trace,
        clock=example.clock,
        id_factory=ids,
    )
    executor, _, _, _, _ = build_executor(
        example, MockTaskExecutionBackend({})
    )
    executor._checkpoints = manager
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    target = checkpoint.model_copy(
        update={
            "checkpoint_revision": 1,
            "last_mutation": CheckpointMutation(
                mutation_id="mutation_uncommitted",
                kind="uncommitted",
                events=(),
            ),
        }
    )
    fail["enabled"] = True
    with pytest.raises(CheckpointPersistenceError) as captured:
        manager.commit(target, expected_revision=0)
    assert not captured.value.checkpoint_replaced

    fail["enabled"] = False
    assert manager.load_and_reconcile(example.state.run_id) == checkpoint


def test_filesystem_checkpoint_round_trip_and_cas(tmp_path) -> None:
    example = runtime_example()
    executor, _, _, _, _ = build_executor(
        example, MockTaskExecutionBackend({})
    )
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    store = FilesystemCheckpointStore(tmp_path)
    store.create(checkpoint)

    assert store.load(checkpoint.run_id) == checkpoint
    target = checkpoint.model_copy(
        update={"checkpoint_revision": 1, "updated_at": example.clock.now()}
    )
    with pytest.raises(CheckpointRevisionConflict):
        store.save(target, expected_revision=4)


def test_checkpoint_store_rejects_instead_of_redacting_dag(tmp_path) -> None:
    example = runtime_example()
    backend = MockTaskExecutionBackend({})
    executor, _, _, _, _ = build_executor(example, backend)
    checkpoint = executor.initialize(
        example.state,
        example.dag,
        example.policy,
        replan_context=example.context,
    )
    tasks = list(checkpoint.dag.tasks)
    tasks[0] = tasks[0].model_copy(update={"objective": "Bearer abcdefghijklmnop"})
    unsafe_dag = checkpoint.dag.model_copy(update={"tasks": tuple(tasks)})
    unsafe = RuntimeCheckpoint.model_validate(
        checkpoint.model_copy(
            update={"dag": unsafe_dag, "dag_hash": model_sha256(unsafe_dag)}
        ).model_dump(mode="python")
    )

    with pytest.raises(UnsafePersistenceData):
        FilesystemCheckpointStore(tmp_path).create(unsafe)
