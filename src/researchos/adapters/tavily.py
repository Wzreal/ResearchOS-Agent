"""Bounded one-dispatch Tavily Search adapter."""

from __future__ import annotations

import asyncio
import json
import math
from contextlib import suppress
from time import monotonic
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
)

from researchos.application.real_composition import (
    BoundRealCapability,
    RealCompositionManager,
)
from researchos.application.real_tool_dispatch import RealToolDispatchAuthorizer
from researchos.domain.identity import sha256_text
from researchos.domain.real_tools import AuthorizedToolDispatchEnvelope
from researchos.domain.runtime import UsageCertainty
from researchos.domain.tools import (
    SearchContentKind,
    SearchHitV2,
    SearchRequest,
    SearchResultV2,
    ToolError,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolUsage,
)
from researchos.domain.web import TAVILY_STANDARD_ENDPOINT
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.runtime import CancellationSignal
from researchos.interfaces.web import BoundedHttpResponse, TavilyHttpTransport


class TavilyTransportError(Exception):
    def __init__(self, *, dispatched: bool, ambiguous: bool = False) -> None:
        super().__init__("Tavily transport failed")
        self.dispatched = dispatched
        self.ambiguous = ambiguous


class _ProviderHit(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    title: str
    url: str
    content: str = ""
    score: StrictFloat | None = None


class _ProviderUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    credits: StrictInt = Field(ge=0, le=100)


class _ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    results: tuple[_ProviderHit, ...]
    usage: _ProviderUsage
    request_id: str | None = None


def canonicalize_search_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("search result URL is not absolute HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("search result URL contains userinfo")
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    port = parsed.port
    default = 80 if parsed.scheme.lower() == "http" else 443
    netloc = host if port in {None, default} else f"{host}:{port}"
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
    )


class TavilySearchTool:
    def __init__(
        self,
        *,
        bound: BoundRealCapability,
        authorizer: RealToolDispatchAuthorizer,
        compositions: RealCompositionManager,
        transport: TavilyHttpTransport,
        clock: Clock,
    ) -> None:
        if bound.settings.tavily_policy is None:
            raise ValueError("Tavily Tool requires Tavily policy")
        self._bound = bound
        self._authorizer = authorizer
        self._compositions = compositions
        self._transport = transport
        self._clock = clock

    @property
    def descriptor(self):
        return self._bound.settings.descriptor()

    @property
    def provider_call_reservation(self):
        return self._bound.settings.provider_reservation

    @property
    def delegates_retry_to_phase3(self) -> bool:
        return True

    async def invoke_authorized(
        self,
        envelope: AuthorizedToolDispatchEnvelope,
        cancellation: CancellationSignal,
    ) -> ToolInvocationResult:
        started = monotonic()
        request = envelope.invocation.input
        policy = self._bound.settings.tavily_policy
        assert policy is not None
        if not isinstance(request, SearchRequest):
            return self._failure("tavily_input_invalid", False, started)
        if cancellation.cancelled:
            return ToolInvocationResult(
                status=ToolInvocationStatus.CANCELLED,
                error=ToolError(
                    code="tavily_cancelled", message="Tavily Search was cancelled"
                ),
                usage=None,
                usage_certainty=UsageCertainty.UNKNOWN,
            )
        if (
            len(request.query) > policy.researchos.max_query_characters
            or len(request.query.encode("utf-8"))
            > policy.researchos.max_query_bytes
            or request.limit > policy.provider_request.max_results
        ):
            return self._failure("tavily_request_bound_exceeded", False, started)
        try:
            self._authorizer.authorize(envelope)
        except Exception:
            return self._failure("real_tool_dispatch_unauthorized", False, started)
        credential = self._compositions.capability_secret("web_search")
        if credential is None:
            return self._failure("tavily_credential_missing", False, started)
        if len(credential) > 4_096 or any(
            ord(character) < 33 or ord(character) > 126
            for character in credential
        ):
            return self._failure("tavily_credential_invalid", False, started)
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if (
            sum(len(key) + len(value) + 4 for key, value in headers.items())
            > policy.researchos.max_header_bytes
        ):
            return self._failure("tavily_request_bound_exceeded", False, started)
        provider = policy.provider_request.model_dump(
            mode="json", exclude={"schema_version"}
        )
        provider["query"] = request.query
        provider["max_results"] = request.limit
        if (
            len(
                json.dumps(
                    provider,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            > policy.researchos.max_request_bytes
        ):
            return self._failure("tavily_request_bound_exceeded", False, started)
        try:
            response = await self._transport.post_json(
                endpoint=f"{TAVILY_STANDARD_ENDPOINT}/search",
                headers=headers,
                payload=provider,
                max_header_bytes=policy.researchos.max_header_bytes,
                max_response_bytes=policy.researchos.max_response_bytes,
                total_timeout_milliseconds=policy.researchos.total_timeout_ms,
                cancellation=cancellation,
            )
        except asyncio.CancelledError:
            return ToolInvocationResult(
                status=ToolInvocationStatus.CANCELLED,
                error=ToolError(
                    code="tavily_cancelled", message="Tavily Search was cancelled"
                ),
                usage=None,
                usage_certainty=UsageCertainty.UNKNOWN,
            )
        except TavilyTransportError as exc:
            certainty = (
                UsageCertainty.UNKNOWN
                if exc.ambiguous
                else UsageCertainty.UPPER_BOUND
                if exc.dispatched
                else UsageCertainty.EXACT
            )
            return self._failure(
                "tavily_transport_failed",
                True,
                started,
                certainty=certainty,
            )
        if response.status_code != 200:
            retryable = response.status_code == 429 or response.status_code >= 500
            return self._failure(
                f"tavily_http_{response.status_code}",
                retryable,
                started,
                certainty=UsageCertainty.UPPER_BOUND,
            )
        content_types = tuple(
            value.lower().split(";", 1)[0]
            for key, value in response.headers
            if key.lower() == "content-type"
        )
        content_encodings = tuple(
            value.lower()
            for key, value in response.headers
            if key.lower() == "content-encoding"
        )
        if (
            content_types != ("application/json",)
            or len(content_encodings) > 1
            or (content_encodings and content_encodings != ("identity",))
        ):
            return self._failure(
                "tavily_response_invalid",
                False,
                started,
                certainty=UsageCertainty.UPPER_BOUND,
            )
        try:
            parsed = _ProviderResponse.model_validate_json(response.body)
            if len(parsed.results) > request.limit:
                raise ValueError("too many Tavily results")
            if parsed.usage.credits > (
                policy.researchos.pricing_profile.maximum_credits_per_call
            ):
                raise ValueError("Tavily credits exceed frozen profile")
            seen: set[str] = set()
            hits = []
            for rank, item in enumerate(parsed.results, start=1):
                canonical = canonicalize_search_url(item.url)
                if canonical in seen:
                    raise ValueError("duplicate Tavily result URL")
                seen.add(canonical)
                if not item.title or len(item.title) > 1_000:
                    raise ValueError("invalid Tavily title")
                if len(item.content) > 10_000:
                    raise ValueError("invalid Tavily snippet")
                provenance: dict[str, Any] = {
                    "provider_id": "tavily",
                    "provider_profile_id": "tavily_standard",
                    "rank": rank,
                    "content_kind": "provider_summary_metadata",
                }
                if item.score is not None:
                    if not math.isfinite(item.score):
                        raise ValueError("invalid Tavily score")
                    provenance["score"] = item.score
                if parsed.request_id:
                    provenance["provider_request_id_hash"] = sha256_text(
                        parsed.request_id
                    )
                hits.append(
                    SearchHitV2(
                        locator=canonical,
                        url=canonical,
                        title=item.title,
                        snippet=item.content,
                        content_kind=SearchContentKind.PROVIDER_SUMMARY_METADATA,
                        retrieved_at=self._clock.now(),
                        adapter_id=self.descriptor.adapter_id,
                        provenance=provenance,
                    )
                )
        except (ValidationError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self._failure(
                "tavily_response_invalid",
                False,
                started,
                certainty=UsageCertainty.UPPER_BOUND,
            )
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResultV2(
                adapter_id=self.descriptor.adapter_id,
                hits=tuple(hits),
            ),
            usage=self._usage(started),
            usage_certainty=UsageCertainty.UPPER_BOUND,
        )

    def _usage(self, started: float) -> ToolUsage:
        return ToolUsage(
            duration_milliseconds=max(0, int((monotonic() - started) * 1_000)),
            cost_microunits=self.provider_call_reservation.cost_microunits,
            tool_calls=1,
        )

    def _failure(
        self,
        code: str,
        retryable: bool,
        started: float,
        *,
        certainty: UsageCertainty = UsageCertainty.EXACT,
    ) -> ToolInvocationResult:
        usage = None if certainty is UsageCertainty.UNKNOWN else self._usage(started)
        if certainty is UsageCertainty.EXACT:
            usage = ToolUsage(
                duration_milliseconds=max(0, int((monotonic() - started) * 1_000)),
                tool_calls=1,
            )
        return ToolInvocationResult(
            status=ToolInvocationStatus.FAILED,
            error=ToolError(
                code=code,
                message="Tavily Search failed",
                retryable=retryable,
            ),
            usage=usage,
            usage_certainty=certainty,
        )


class HttpxTavilyTransport:
    def __init__(self, policy) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Tavily REAL extra is unavailable") from exc
        self._httpx = httpx
        self._policy = policy

    async def post_json(self, **values) -> BoundedHttpResponse:
        max_bytes = values.pop("max_response_bytes")
        max_header_bytes = values.pop("max_header_bytes")
        total_timeout = values.pop("total_timeout_milliseconds")
        cancellation = values.pop("cancellation")
        if cancellation.cancelled:
            raise asyncio.CancelledError
        operation = asyncio.create_task(self._post(values, max_bytes, max_header_bytes))
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, cancelled},
                timeout=total_timeout / 1_000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                operation.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await operation
                raise asyncio.CancelledError
            if operation not in done:
                operation.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await operation
                raise TavilyTransportError(dispatched=True, ambiguous=True)
            return operation.result()
        except TavilyTransportError:
            raise
        except self._httpx.HTTPError as exc:
            raise TavilyTransportError(dispatched=True, ambiguous=True) from exc
        finally:
            cancelled.cancel()
            with suppress(asyncio.CancelledError):
                await cancelled

    async def _post(self, values, max_bytes, max_header_bytes):
        timeout = self._httpx.Timeout(
            connect=self._policy.connect_timeout_ms / 1_000,
            read=self._policy.read_timeout_ms / 1_000,
            write=self._policy.write_timeout_ms / 1_000,
            pool=self._policy.pool_timeout_ms / 1_000,
        )
        async with self._httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client, client.stream(
            "POST",
            values["endpoint"],
            headers=values["headers"],
            json=values["payload"],
        ) as response:
            if (
                sum(
                    len(key) + len(value) + 4
                    for key, value in response.headers.items()
                )
                > max_header_bytes
            ):
                raise TavilyTransportError(dispatched=True)
            body = bytearray()
            async for chunk in response.aiter_raw():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise TavilyTransportError(dispatched=True)
            return BoundedHttpResponse(
                status_code=response.status_code,
                headers=tuple(response.headers.items()),
                body=bytes(body),
            )
