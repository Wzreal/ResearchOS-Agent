"""Deterministic stdlib BM25 retrieval over a validated local JSONL corpus."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from researchos.application.errors import MalformedCorpusError
from researchos.domain.contracts import SafeId
from researchos.domain.tools import (
    AdapterMode,
    LocalRetrievalHit,
    LocalRetrievalRequest,
    LocalRetrievalResult,
    ToolDescriptor,
    ToolError,
    ToolInvocationRequest,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.runtime import CancellationSignal


class _CorpusChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    document_id: SafeId
    chunk_id: SafeId
    locator: str
    content: str
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("locator", "content")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("corpus text fields must not be blank")
        return value

    @field_validator("source_metadata")
    @classmethod
    def metadata_is_finite_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        json.dumps(value, allow_nan=False)
        return value


class LocalRetrievalTool:
    def __init__(
        self,
        *,
        root: Path,
        corpus_path: str,
        clock: Clock,
        tool_id: str = "local_retrieval",
        adapter_id: str = "stdlib_bm25",
    ) -> None:
        self._root = root.resolve()
        self._corpus_path = self._resolve_corpus(corpus_path)
        self._clock = clock
        self._descriptor = ToolDescriptor(
            tool_id=tool_id,
            capability_id="local_retrieval",
            adapter_id=adapter_id,
            mode=AdapterMode.LOCAL,
            operation_version="bm25-v1",
            input_type="local_retrieval",
            output_type="local_retrieval_result",
            side_effect=ToolSideEffect.NONE,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    async def invoke(
        self, request: ToolInvocationRequest, cancellation: CancellationSignal
    ) -> ToolInvocationResult:
        if cancellation.cancelled:
            return _failure("tool_cancelled", "retrieval was cancelled", "cancelled")
        if not isinstance(request.input, LocalRetrievalRequest):
            return _failure("invalid_tool_input", "retrieval input is invalid")
        try:
            chunks = self._load()
        except MalformedCorpusError:
            return _failure("malformed_corpus", "local corpus is malformed")
        query_tokens = _tokens(request.input.query)
        ranked = _rank(chunks, query_tokens)
        hits = tuple(
            LocalRetrievalHit(
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
                locator=chunk.locator,
                content=chunk.content,
                content_hash=hashlib.sha256(
                    chunk.content.encode("utf-8")
                ).hexdigest(),
                score=score,
                source_metadata=chunk.source_metadata,
            )
            for score, chunk in ranked[: request.input.limit]
        )
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=LocalRetrievalResult(
                adapter_id=self._descriptor.adapter_id,
                retrieved_at=self._clock.now(),
                hits=hits,
                matched_count=len(ranked),
            ),
            usage=ToolUsage(tool_calls=1),
            usage_certainty="exact",
        )

    def _resolve_corpus(self, value: str) -> Path:
        if "\\" in value:
            raise ValueError("corpus path must use POSIX separators")
        pure = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if (
            pure.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("corpus path must be a safe relative path")
        candidate = (self._root / value).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError("corpus path escapes allowed root")
        return candidate

    def _load(self) -> tuple[_CorpusChunk, ...]:
        try:
            lines = self._corpus_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise MalformedCorpusError("cannot read local corpus") from exc
        chunks: list[_CorpusChunk] = []
        try:
            for line in lines:
                if line.strip():
                    chunks.append(_CorpusChunk.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise MalformedCorpusError("invalid local corpus record") from exc
        identities = [(item.document_id, item.chunk_id) for item in chunks]
        if len(identities) != len(set(identities)):
            raise MalformedCorpusError("duplicate local corpus chunk")
        return tuple(chunks)


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    words = re.findall(r"[a-z0-9_]+", normalized)
    cjk_sequences = re.findall(r"[\u3400-\u9fff]+", normalized)
    cjk = [
        sequence[index : index + 2]
        for sequence in cjk_sequences
        for index in range(max(1, len(sequence) - 1))
    ]
    return tuple(words + cjk)


def _rank(
    chunks: tuple[_CorpusChunk, ...], query: tuple[str, ...]
) -> list[tuple[float, _CorpusChunk]]:
    if not chunks or not query:
        return []
    tokenized = [_tokens(item.content) for item in chunks]
    average_length = sum(len(item) for item in tokenized) / len(tokenized) or 1
    document_frequency = Counter(
        token for tokens in tokenized for token in set(tokens)
    )
    ranked: list[tuple[float, _CorpusChunk]] = []
    for chunk, tokens in zip(chunks, tokenized, strict=True):
        frequencies = Counter(tokens)
        score = 0.0
        for token in query:
            frequency = frequencies[token]
            if not frequency:
                continue
            df = document_frequency[token]
            inverse = math.log(1 + (len(chunks) - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.2 * (
                1 - 0.75 + 0.75 * len(tokens) / average_length
            )
            score += inverse * frequency * 2.2 / denominator
        rounded = round(score, 12)
        if rounded > 0:
            ranked.append((rounded, chunk))
    ranked.sort(key=lambda item: (-item[0], item[1].document_id, item[1].chunk_id))
    return ranked


def _failure(code: str, message: str, status: str = "failed"):
    return ToolInvocationResult(
        status=status,
        error=ToolError(code=code, message=message),
        usage=ToolUsage(tool_calls=1),
        usage_certainty="exact",
    )
