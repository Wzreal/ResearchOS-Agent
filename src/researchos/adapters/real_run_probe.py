"""Filesystem proof that a missing REAL composition may be bootstrapped."""

from __future__ import annotations

from pathlib import Path

from researchos.domain.contracts import RunState, TraceEventType
from researchos.interfaces.lifecycle import TraceSink

_CREATION_TRACE_TYPES = frozenset(
    {
        TraceEventType.TRANSITION_INTENT,
        TraceEventType.TRANSITION_COMMITTED,
        TraceEventType.TRANSITION_RECONCILED,
    }
)
_FORBIDDEN_ARTIFACT_NAMES = (
    "checkpoint.json",
    "evidence.jsonl",
    "claims.jsonl",
    "verification.json",
    "evaluation.json",
    "claim_extraction.json",
    "verification_operations",
)


class FilesystemPristineRealRunProbe:
    def __init__(self, root: str | Path, *, trace_sink: TraceSink) -> None:
        self._root = Path(root).resolve()
        self._trace = trace_sink

    def is_pristine(self, state: RunState) -> bool:
        if state.revision != 0 or state.status.value != "created":
            return False
        run_root = (self._root / state.run_id).resolve()
        if run_root.parent != self._root:
            return False
        if any((run_root / name).exists() for name in _FORBIDDEN_ARTIFACT_NAMES):
            return False
        try:
            events = self._trace.read(state.run_id, recover_torn_tail=False)
        except Exception:
            return False
        return bool(events) and all(
            event.revision == 0
            and event.event_type in _CREATION_TRACE_TYPES
            and event.run_id == state.run_id
            for event in events
        )
