"""Small immutable helpers for checkpoint task replacement and hashing."""

from __future__ import annotations

import hashlib
import json

from researchos.application.errors import RuntimePreconditionError
from researchos.domain.runtime import (
    AttemptStatus,
    RuntimeCheckpoint,
    TaskRuntimeState,
    TaskStatus,
)

LEGAL_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.RETRY_WAIT,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RETRY_WAIT: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.BLOCKED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

LEGAL_ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.INTERRUPTED,
        }
    ),
    AttemptStatus.SUCCEEDED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.TIMED_OUT: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
    AttemptStatus.INTERRUPTED: frozenset(),
}


def validate_task_transition(previous: TaskStatus, target: TaskStatus) -> None:
    if target not in LEGAL_TASK_TRANSITIONS[previous]:
        raise RuntimePreconditionError(
            f"illegal task transition: {previous.value} -> {target.value}"
        )


def validate_attempt_transition(previous: AttemptStatus, target: AttemptStatus) -> None:
    if target not in LEGAL_ATTEMPT_TRANSITIONS[previous]:
        raise RuntimePreconditionError(
            f"illegal attempt transition: {previous.value} -> {target.value}"
        )


def replace_task(
    checkpoint: RuntimeCheckpoint, replacement: TaskRuntimeState
) -> tuple[TaskRuntimeState, ...]:
    return tuple(
        replacement if item.task_id == replacement.task_id else item
        for item in checkpoint.task_states
    )


def stable_key(parts: dict[str, object]) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
