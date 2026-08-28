"""Deterministic Phase 5 JSONL snapshot envelope helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from researchos.domain.contracts import ContractModel


def canonical_dict_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encode_snapshot(
    *,
    run_id: str,
    store_revision: int,
    schema_version: int,
    records: Iterable[tuple[str, ContractModel]],
) -> bytes:
    encoded_records = [
        canonical_dict_bytes(
            {"record_type": kind, "payload": model.model_dump(mode="json")}
        )
        + b"\n"
        for kind, model in records
    ]
    payload = b"".join(encoded_records)
    header = {
        "format": "researchos-jsonl-snapshot-v1",
        "run_id": run_id,
        "schema_version": schema_version,
        "store_revision": store_revision,
        "record_count": len(encoded_records),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    return canonical_dict_bytes(header) + b"\n" + payload


def decode_snapshot(
    data: bytes,
) -> tuple[dict[str, Any], tuple[tuple[str, dict[str, Any]], ...]]:
    if not data.endswith(b"\n"):
        raise ValueError("snapshot lacks complete trailing newline")
    lines = data.splitlines(keepends=True)
    if not lines:
        raise ValueError("snapshot is empty")
    header = json.loads(lines[0])
    header_fields = {
        "format",
        "run_id",
        "schema_version",
        "store_revision",
        "record_count",
        "payload_sha256",
    }
    if not isinstance(header, dict) or set(header) != header_fields:
        raise ValueError("snapshot header fields are invalid")
    if header.get("format") != "researchos-jsonl-snapshot-v1":
        raise ValueError("snapshot header is invalid")
    if (
        not isinstance(header.get("run_id"), str)
        or not isinstance(header.get("schema_version"), int)
        or not isinstance(header.get("store_revision"), int)
        or header["store_revision"] < 0
        or not isinstance(header.get("record_count"), int)
        or header["record_count"] < 0
    ):
        raise ValueError("snapshot header types are invalid")
    payload = b"".join(lines[1:])
    if header.get("record_count") != len(lines) - 1:
        raise ValueError("snapshot record count differs")
    if header.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
        raise ValueError("snapshot payload hash differs")
    records: list[tuple[str, dict[str, Any]]] = []
    for line in lines[1:]:
        wrapper = json.loads(line)
        if not isinstance(wrapper, dict) or set(wrapper) != {"record_type", "payload"}:
            raise ValueError("snapshot record envelope is invalid")
        if not isinstance(wrapper["record_type"], str) or not isinstance(
            wrapper["payload"], dict
        ):
            raise ValueError("snapshot record fields are invalid")
        records.append((wrapper["record_type"], wrapper["payload"]))
    return header, tuple(records)
