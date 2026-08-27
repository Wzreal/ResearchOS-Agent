"""Minimal intent-snapshot-settle protocol for runtime checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from researchos.application.errors import (
    CorruptCheckpoint,
    RuntimeTraceCommitError,
)
from researchos.application.runtime_transitions import stable_key
from researchos.domain.contracts import TraceEvent, TraceEventType, model_sha256
from researchos.domain.runtime import RuntimeCheckpoint, TraceEventDescriptor
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.interfaces.runtime import CheckpointStore
from researchos.security.redaction import PersistenceRedactor

IdFactory = Callable[[str], str]


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class CheckpointManager:
    """Single-writer snapshot coordinator with one immutable trace outbox."""

    def __init__(
        self,
        *,
        store: CheckpointStore,
        trace_sink: TraceSink,
        clock: Clock,
        id_factory: IdFactory | None = None,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._store = store
        self._trace = trace_sink
        self._clock = clock
        self._id_factory = id_factory or _default_id_factory
        self._redactor = redactor or PersistenceRedactor()

    def descriptor(self, event: TraceEvent) -> TraceEventDescriptor:
        self._redactor.assert_safe_model(event)
        return TraceEventDescriptor.from_event(event)

    def create(self, checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
        if checkpoint.checkpoint_revision != 0:
            raise CorruptCheckpoint("initial checkpoint revision must be zero")
        self._assert_checkpoint_safe(checkpoint)
        intent = self._protocol_event(
            checkpoint, TraceEventType.CHECKPOINT_INTENT, phase="intent"
        )
        self._trace.append(intent)
        self._store.create(checkpoint)
        self._settle(checkpoint, intent=intent)
        return checkpoint

    def commit(
        self, checkpoint: RuntimeCheckpoint, *, expected_revision: int
    ) -> RuntimeCheckpoint:
        if checkpoint.checkpoint_revision != expected_revision + 1:
            raise CorruptCheckpoint("checkpoint mutation must increment revision once")
        self._assert_checkpoint_safe(checkpoint)
        # A previous one-mutation outbox must be fully settled before it can be
        # replaced; this is what keeps the mechanism smaller than a WAL.
        current = self._store.load(checkpoint.run_id)
        if current.checkpoint_revision != expected_revision:
            raise CorruptCheckpoint("checkpoint changed before mutation")
        self.reconcile(current)
        intent = self._protocol_event(
            checkpoint, TraceEventType.CHECKPOINT_INTENT, phase="intent"
        )
        self._trace.append(intent)
        self._store.save(checkpoint, expected_revision=expected_revision)
        self._settle(checkpoint, intent=intent)
        return checkpoint

    def load_and_reconcile(self, run_id: str) -> RuntimeCheckpoint:
        checkpoint = self._store.load(run_id)
        self._assert_checkpoint_safe(checkpoint)
        self.reconcile(checkpoint)
        return checkpoint

    def reconcile(self, checkpoint: RuntimeCheckpoint) -> None:
        events = self._trace.read(checkpoint.run_id, recover_torn_tail=True)
        by_id: dict[str, TraceEvent] = {}
        for event in events:
            existing = by_id.get(event.event_id)
            if existing is not None and model_sha256(existing) != model_sha256(event):
                raise CorruptCheckpoint("same trace event ID has different content")
            if existing is not None:
                raise CorruptCheckpoint("trace contains duplicate event ID")
            by_id[event.event_id] = event

        revision = checkpoint.checkpoint_revision
        mutation_id = checkpoint.last_mutation.mutation_id
        intents = self._protocol_matches(
            events, TraceEventType.CHECKPOINT_INTENT, revision, mutation_id
        )
        commits = self._protocol_matches(
            events, TraceEventType.CHECKPOINT_COMMITTED, revision, mutation_id
        )
        reconciled = self._protocol_matches(
            events, TraceEventType.CHECKPOINT_RECONCILED, revision, mutation_id
        )
        if len(intents) != 1:
            raise CorruptCheckpoint("checkpoint lacks exactly one matching intent")
        if len(commits) > 1 or len(reconciled) > 1 or (commits and reconciled):
            raise CorruptCheckpoint("checkpoint has invalid settlement events")

        for event in events:
            if event.event_type in {
                TraceEventType.CHECKPOINT_COMMITTED,
                TraceEventType.CHECKPOINT_RECONCILED,
            }:
                event_revision = event.attributes.get("checkpoint_revision")
                if isinstance(event_revision, int) and event_revision > revision:
                    raise CorruptCheckpoint("trace settles a newer checkpoint")

        missing: list[TraceEventDescriptor] = []
        for descriptor in checkpoint.last_mutation.events:
            existing = by_id.get(descriptor.event_id)
            if existing is None:
                missing.append(descriptor)
            elif model_sha256(existing) != descriptor.canonical_event_hash:
                raise CorruptCheckpoint("persisted outbox event content differs")
        if commits or reconciled:
            if missing:
                raise CorruptCheckpoint("settled checkpoint is missing outbox events")
            return

        # The snapshot is the committed fact. Replay exact stored descriptors,
        # including their original timestamps, then record distinct recovery.
        for descriptor in missing:
            self._trace.append(descriptor.to_event())
        recovery = self._protocol_event(
            checkpoint,
            TraceEventType.CHECKPOINT_RECONCILED,
            phase="reconciled",
            causation_id=intents[0].event_id,
            attributes={"replayed_event_count": len(missing)},
        )
        self._trace.append(recovery)

    def _settle(self, checkpoint: RuntimeCheckpoint, *, intent: TraceEvent) -> None:
        try:
            for descriptor in checkpoint.last_mutation.events:
                self._trace.append(descriptor.to_event())
            self._trace.append(
                self._protocol_event(
                    checkpoint,
                    TraceEventType.CHECKPOINT_COMMITTED,
                    phase="committed",
                    causation_id=intent.event_id,
                )
            )
        except Exception as exc:
            raise RuntimeTraceCommitError(
                "checkpoint committed but trace settlement was not confirmed",
                run_id=checkpoint.run_id,
                checkpoint_revision=checkpoint.checkpoint_revision,
            ) from exc

    def _protocol_event(
        self,
        checkpoint: RuntimeCheckpoint,
        event_type: TraceEventType,
        *,
        phase: str,
        causation_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> TraceEvent:
        mutation_id = checkpoint.last_mutation.mutation_id
        suffix = {
            "intent": "intent",
            "committed": "commit",
            "reconciled": "reconcile",
        }[phase]
        values: dict[str, Any] = {
            "checkpoint_revision": checkpoint.checkpoint_revision,
            "mutation_id": mutation_id,
            "mutation_kind": checkpoint.last_mutation.kind,
        }
        values.update(attributes or {})
        protocol_event_key = stable_key(
            {
                "run_id": checkpoint.run_id,
                "checkpoint_revision": checkpoint.checkpoint_revision,
                "mutation_id": mutation_id,
                "phase": phase,
            }
        )
        return TraceEvent(
            event_id=f"evt_cp_{suffix}_{protocol_event_key[:32]}",
            event_type=event_type,
            timestamp=self._clock.now(),
            run_id=checkpoint.run_id,
            revision=checkpoint.run_revision,
            previous_status=None,
            next_status=None,
            correlation_id=mutation_id,
            causation_id=causation_id,
            attributes=values,
        )

    @staticmethod
    def _protocol_matches(
        events: tuple[TraceEvent, ...],
        event_type: TraceEventType,
        checkpoint_revision: int,
        mutation_id: str,
    ) -> list[TraceEvent]:
        return [
            event
            for event in events
            if event.event_type is event_type
            and event.attributes.get("checkpoint_revision") == checkpoint_revision
            and event.attributes.get("mutation_id") == mutation_id
        ]

    def _assert_checkpoint_safe(self, checkpoint: RuntimeCheckpoint) -> None:
        self._redactor.assert_safe_model(checkpoint)
        for descriptor in checkpoint.last_mutation.events:
            self._redactor.assert_safe_model(descriptor.to_event())

    def new_id(self, prefix: str) -> str:
        return self._id_factory(prefix)
