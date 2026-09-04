"""Narrow Phase 9B web transport and resolver ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from researchos.interfaces.runtime import CancellationSignal


@dataclass(frozen=True, slots=True)
class BoundedHttpResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class TavilyHttpTransport(Protocol):
    async def post_json(
        self,
        *,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, object],
        max_header_bytes: int,
        max_response_bytes: int,
        total_timeout_milliseconds: int,
        cancellation: CancellationSignal,
    ) -> BoundedHttpResponse: ...


class HostResolver(Protocol):
    async def resolve(
        self,
        host: str,
        port: int,
        *,
        cancellation: CancellationSignal,
    ) -> tuple[str, ...]: ...


class BrowserDocumentExtractor(Protocol):
    async def extract(
        self,
        content: bytes,
        *,
        media_type: str,
        charset: str,
        timeout_milliseconds: int,
        max_characters: int,
        cancellation: CancellationSignal,
    ) -> tuple[str, str]: ...


class BrowserHttpTransport(Protocol):
    async def get(
        self,
        *,
        url: str,
        max_header_bytes: int,
        max_body_bytes: int,
        connect_timeout_milliseconds: int,
        read_timeout_milliseconds: int,
        content_encoding_policy: str,
        user_agent: str,
        cancellation: CancellationSignal,
    ) -> BoundedHttpResponse: ...
