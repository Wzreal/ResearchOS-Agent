"""Provider-independent Phase 4 tool and adapter contracts."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.contracts import (
    ContractModel,
    SafeId,
    Sha256,
    _require_aware,
)
from researchos.domain.provider_diagnostics import validate_tool_diagnostics
from researchos.domain.runtime import IdempotencyMode, UsageCertainty

TOOL_SCHEMA_VERSION = 1
NonBlank = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]


class AdapterMode(StrEnum):
    MOCK = "mock"
    LOCAL = "local"
    REAL = "real"


class ToolSideEffect(StrEnum):
    NONE = "none"
    LOCAL_ARTIFACT = "local_artifact"
    EXTERNAL = "external"


class ToolInvocationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ToolDescriptor(ContractModel):
    schema_version: Literal[TOOL_SCHEMA_VERSION] = TOOL_SCHEMA_VERSION
    tool_id: SafeId
    capability_id: SafeId
    adapter_id: SafeId
    mode: AdapterMode
    operation_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    input_type: SafeId
    output_type: SafeId
    side_effect: ToolSideEffect = ToolSideEffect.NONE
    idempotency: IdempotencyMode = IdempotencyMode.IDEMPOTENT


class ToolUsage(ContractModel):
    duration_milliseconds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_microunits: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=1, ge=0)


class ToolError(ContractModel):
    code: SafeId
    message: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def details_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        json.dumps(value, allow_nan=False)
        return value


class ToolArtifact(ContractModel):
    artifact_id: SafeId
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    relative_path: Annotated[str, StringConstraints(min_length=1, max_length=1_024)]
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    producer_tool_id: SafeId
    tool_operation_key: Sha256

    @field_validator("relative_path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("artifact path must use POSIX separators")
        parts = value.split("/")
        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("artifact path must be a safe relative path")
        return value


class LocalRetrievalRequest(ContractModel):
    input_type: Literal["local_retrieval"] = "local_retrieval"
    query: Annotated[str, StringConstraints(min_length=1, max_length=4_000)]
    limit: int = Field(default=5, ge=1, le=100)


class LocalRetrievalHit(ContractModel):
    document_id: SafeId
    chunk_id: SafeId
    locator: Annotated[str, StringConstraints(min_length=1, max_length=1_024)]
    content: NonBlank
    content_hash: Sha256
    score: float = Field(ge=0)
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_metadata")
    @classmethod
    def source_metadata_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        json.dumps(value, allow_nan=False)
        return value


class LocalRetrievalResult(ContractModel):
    output_type: Literal["local_retrieval_result"] = "local_retrieval_result"
    adapter_id: SafeId
    retrieved_at: datetime
    hits: tuple[LocalRetrievalHit, ...]
    matched_count: int = Field(ge=0)

    _aware = field_validator("retrieved_at")(_require_aware)


class PythonInputFile(ContractModel):
    relative_path: Annotated[str, StringConstraints(min_length=1, max_length=1_024)]
    content: str

    _safe_path = field_validator("relative_path")(ToolArtifact.path_is_safe.__func__)


class PythonExecutionRequest(ContractModel):
    input_type: Literal["python"] = "python"
    source: NonBlank
    input_files: tuple[PythonInputFile, ...] = ()
    artifact_paths: tuple[str, ...] = ()

    @field_validator("artifact_paths")
    @classmethod
    def artifact_paths_are_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("artifact paths must be unique")
        for item in value:
            ToolArtifact.path_is_safe(item)
        return value


class PythonExecutionResult(ContractModel):
    output_type: Literal["python_result"] = "python_result"
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class SearchRequest(ContractModel):
    input_type: Literal["search"] = "search"
    query: Annotated[str, StringConstraints(min_length=1, max_length=4_000)]
    limit: int = Field(default=5, ge=1, le=100)


class SearchHit(ContractModel):
    locator: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    url: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    snippet: Annotated[str, StringConstraints(max_length=10_000)]
    retrieved_at: datetime
    adapter_id: SafeId
    provenance: dict[str, Any] = Field(default_factory=dict)

    _aware = field_validator("retrieved_at")(_require_aware)

    @field_validator("url")
    @classmethod
    def url_is_http(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL must be absolute HTTP(S)")
        return value

    @field_validator("provenance")
    @classmethod
    def provenance_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        json.dumps(value, allow_nan=False)
        return value


class SearchResult(ContractModel):
    output_type: Literal["search_result"] = "search_result"
    adapter_id: SafeId
    hits: tuple[SearchHit, ...]


class SearchContentKind(StrEnum):
    SOURCE_EXCERPT = "source_excerpt"
    PROVIDER_SUMMARY_METADATA = "provider_summary_metadata"


class SearchHitV2(ContractModel):
    locator: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    url: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    snippet: Annotated[str, StringConstraints(max_length=10_000)]
    content_kind: SearchContentKind
    retrieved_at: datetime
    adapter_id: SafeId
    provenance: dict[str, Any] = Field(default_factory=dict)

    _aware = field_validator("retrieved_at")(_require_aware)
    _http_url = field_validator("url")(SearchHit.url_is_http.__func__)

    @field_validator("provenance")
    @classmethod
    def provenance_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        json.dumps(value, allow_nan=False)
        return value


class SearchResultV2(ContractModel):
    output_type: Literal["search_result_v2"] = "search_result_v2"
    adapter_id: SafeId
    hits: tuple[SearchHitV2, ...]


class BrowserRequest(ContractModel):
    input_type: Literal["browser"] = "browser"
    url: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    locator: (
        Annotated[str, StringConstraints(min_length=1, max_length=1_024)] | None
    ) = None

    _http_url = field_validator("url")(SearchHit.url_is_http.__func__)


class BrowserResult(ContractModel):
    output_type: Literal["browser_result"] = "browser_result"
    adapter_id: SafeId
    final_url: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    content: NonBlank
    content_hash: Sha256
    retrieved_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)

    _aware = field_validator("retrieved_at")(_require_aware)
    _http_url = field_validator("final_url")(SearchHit.url_is_http.__func__)

    @field_validator("provenance")
    @classmethod
    def provenance_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        json.dumps(value, allow_nan=False)
        return value


ToolInput = Annotated[
    LocalRetrievalRequest | PythonExecutionRequest | SearchRequest | BrowserRequest,
    Field(discriminator="input_type"),
]
ToolOutput = Annotated[
    LocalRetrievalResult
    | PythonExecutionResult
    | SearchResult
    | SearchResultV2
    | BrowserResult,
    Field(discriminator="output_type"),
]


class ToolInvocationRequest(ContractModel):
    invocation_id: SafeId
    run_id: SafeId
    task_id: SafeId
    attempt_id: SafeId
    agent_step: int = Field(ge=1)
    tool_call_id: SafeId
    tool_operation_key: Sha256
    deadline: datetime
    input: ToolInput

    _aware = field_validator("deadline")(_require_aware)


class ToolInvocationResult(ContractModel):
    status: ToolInvocationStatus
    output: ToolOutput | None = None
    error: ToolError | None = None
    artifacts: tuple[ToolArtifact, ...] = ()
    usage: ToolUsage | None = None
    usage_certainty: UsageCertainty = UsageCertainty.UNKNOWN
    provider_diagnostics: dict[str, Any] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def result_is_consistent(self) -> ToolInvocationResult:
        succeeded = self.status is ToolInvocationStatus.SUCCEEDED
        if succeeded != (self.output is not None) or succeeded == (
            self.error is not None
        ):
            raise ValueError(
                "tool success requires output; other statuses require error"
            )
        if self.usage_certainty is UsageCertainty.UNKNOWN and self.usage is not None:
            raise ValueError("unknown tool usage cannot contain an amount")
        if self.usage_certainty is not UsageCertainty.UNKNOWN and self.usage is None:
            raise ValueError("known tool usage requires an amount")
        if not succeeded and self.artifacts:
            raise ValueError("failed tool invocation cannot publish artifacts")
        return self

    @field_validator("provider_diagnostics")
    @classmethod
    def provider_diagnostics_are_json(cls, value: dict[str, Any] | None):
        return validate_tool_diagnostics(value)
