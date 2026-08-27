"""Private same-directory atomic file primitive shared by local stores."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4


class AtomicWriteFailure(Exception):
    def __init__(self, *, replaced: bool) -> None:
        super().__init__("atomic file replacement failed")
        self.replaced = replaced


def fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(descriptor)


def atomic_replace_bytes(
    path: Path,
    data: bytes,
    *,
    fault: Callable[[str], None],
) -> bool:
    """Write and replace, returning only after parent durability is attempted.

    The typed failure preserves whether replacement returned successfully.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    replaced = False
    try:
        fault("before_temp_write")
        with temp_path.open("xb") as handle:
            handle.write(data)
            fault("after_temp_write")
            handle.flush()
            fault("after_temp_flush")
            os.fsync(handle.fileno())
            fault("after_temp_fsync")
        fault("before_replace")
        os.replace(temp_path, path)
        replaced = True
        fault("after_replace")
        fsync_parent(path.parent)
        fault("after_parent_fsync")
    except Exception as exc:
        raise AtomicWriteFailure(replaced=replaced) from exc
    finally:
        if not replaced and temp_path.exists():
            temp_path.unlink()
    return replaced
