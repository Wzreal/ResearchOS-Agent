"""Small immutable helpers for checkpoint task replacement and hashing."""

from __future__ import annotations

import hashlib
import json

from researchos.domain.runtime import RuntimeCheckpoint, TaskRuntimeState


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
