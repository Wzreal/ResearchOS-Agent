"""Bounded raw-HTTP OpenAI-compatible chat transport."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from researchos.application.errors import (
    MissingOptionalDependency,
    RealProviderFailure,
)
from researchos.application.real_composition import BoundRealModel
from researchos.domain.real_composition import SamplingControlMode
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.interfaces.providers import ProviderDispatchDiagnostic
from researchos.interfaces.runtime import CancellationSignal


class _SyncResponse(Protocol):
    status_code: int

    def iter_bytes(self) -> Any: ...


class _AsyncResponse(Protocol):
    status_code: int

    def aiter_bytes(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class ChatTransportResponse:
    content: bytes
    model_id: str
    usage: RuntimeResourceAmount
    usage_certainty: UsageCertainty
    diagnostic: ProviderDispatchDiagnostic = (
        ProviderDispatchDiagnostic.RESPONSE_RECEIVED
    )


def _load_httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:
        raise MissingOptionalDependency("llm") from exc
    return httpx


class OpenAICompatibleChatTransport:
    """One-call transport with no retry or persistence behavior."""

    def __init__(
        self,
        bound: BoundRealModel,
        *,
        sync_client: Any | None = None,
        async_client: Any | None = None,
    ) -> None:
        self._bound = bound
        self._sync_client = sync_client
        self._async_client = async_client
        needs_sync = bound.settings.role_id == "planning"
        needs_async = bound.settings.role_id in {"agent", "verification"}
        self._owns_sync_client = sync_client is None and needs_sync
        self._owns_async_client = async_client is None and needs_async
        self._sync_closed = False
        self._async_closed = False
        if self._owns_sync_client or self._owns_async_client:
            httpx = _load_httpx()
            timeout = httpx.Timeout(
                connect=bound.settings.policy.connect_timeout_ms / 1_000,
                read=bound.settings.policy.read_timeout_ms / 1_000,
                write=bound.settings.policy.write_timeout_ms / 1_000,
                pool=bound.settings.policy.pool_timeout_ms / 1_000,
            )
            if self._owns_sync_client:
                self._sync_client = httpx.Client(
                    timeout=timeout, follow_redirects=False, trust_env=False
                )
            if self._owns_async_client:
                self._async_client = httpx.AsyncClient(
                    timeout=timeout, follow_redirects=False, trust_env=False
                )

    @property
    def bound(self) -> BoundRealModel:
        return self._bound

    def close(self) -> None:
        if not self._owns_sync_client or self._sync_closed:
            return
        try:
            assert self._sync_client is not None
            self._sync_client.close()
        except Exception:
            failure = RealProviderFailure(
                "provider_client_close_failed",
                diagnostic=ProviderDispatchDiagnostic.NOT_DISPATCHED,
            )
        else:
            self._sync_closed = True
            failure = None
        if failure is not None:
            raise failure

    async def aclose(self) -> None:
        if not self._owns_async_client or self._async_closed:
            return
        try:
            assert self._async_client is not None
            await self._async_client.aclose()
        except Exception:
            failure = RealProviderFailure(
                "provider_client_close_failed",
                diagnostic=ProviderDispatchDiagnostic.NOT_DISPATCHED,
            )
        else:
            self._async_closed = True
            failure = None
        if failure is not None:
            raise failure

    def complete(self, messages: tuple[dict[str, str], ...]) -> ChatTransportResponse:
        request = self._request_bytes(messages)
        started = time.perf_counter_ns()
        failure: RealProviderFailure | None = None
        try:
            assert self._sync_client is not None
            with self._sync_client.stream(
                "POST",
                self._url(),
                content=request,
                headers=self._headers(),
            ) as response:
                raw = self._read_sync(response)
        except RealProviderFailure:
            raise
        except Exception:
            failure = RealProviderFailure(
                "provider_transport_failed",
                diagnostic=ProviderDispatchDiagnostic.DISPATCHED_OUTCOME_UNKNOWN,
                retryable=True,
            )
        if failure is not None:
            raise failure
        return self._parse(raw, response.status_code, started)

    async def complete_async(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        cancellation: CancellationSignal,
        deadline: datetime | None = None,
    ) -> ChatTransportResponse:
        if cancellation.cancelled:
            raise RealProviderFailure(
                "provider_cancelled_before_dispatch",
                diagnostic=ProviderDispatchDiagnostic.NOT_DISPATCHED,
            )
        if deadline is not None and deadline <= datetime.now(deadline.tzinfo):
            raise RealProviderFailure(
                "provider_deadline_expired_before_dispatch",
                diagnostic=ProviderDispatchDiagnostic.NOT_DISPATCHED,
            )
        request = self._request_bytes(messages)
        started = time.perf_counter_ns()
        operation = asyncio.create_task(
            self._send_async(request), name="researchos-provider-operation"
        )
        cancelled = asyncio.create_task(
            cancellation.wait(), name="researchos-provider-cancellation-waiter"
        )
        provider_total_timeout = (
            self._bound.settings.policy.provider_total_call_timeout_ms
        )
        timeout: float | None = (
            provider_total_timeout / 1_000
            if provider_total_timeout is not None
            else None
        )
        if deadline is not None:
            now = datetime.now(deadline.tzinfo)
            deadline_timeout = max(0.0, (deadline - now).total_seconds())
            timeout = (
                deadline_timeout
                if timeout is None
                else min(timeout, deadline_timeout)
            )
        try:
            done, _ = await asyncio.wait(
                {operation, cancelled},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation not in done:
                await self._cancel_children(operation, cancelled)
                if cancellation.cancelled:
                    code = "provider_cancelled"
                elif deadline is not None and (
                    provider_total_timeout is None
                    or timeout < provider_total_timeout / 1_000
                ):
                    code = "provider_deadline_exceeded"
                else:
                    code = "provider_total_call_timeout"
                raise RealProviderFailure(
                    code,
                    diagnostic=(
                        ProviderDispatchDiagnostic.DISPATCHED_OUTCOME_UNKNOWN
                    ),
                )
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            failure = None
            try:
                raw, status_code = await operation
            except RealProviderFailure:
                raise
            except Exception:
                failure = RealProviderFailure(
                    "provider_transport_failed",
                    diagnostic=(
                        ProviderDispatchDiagnostic.DISPATCHED_OUTCOME_UNKNOWN
                    ),
                    retryable=True,
                )
            if failure is not None:
                raise failure
            return self._parse(raw, status_code, started)
        except asyncio.CancelledError:
            await self._cancel_children(operation, cancelled)
            raise

    @staticmethod
    async def _cancel_children(*tasks: asyncio.Task[Any]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_async(self, request: bytes) -> tuple[bytes, int]:
        failure: RealProviderFailure | None = None
        try:
            assert self._async_client is not None
            async with self._async_client.stream(
                "POST",
                self._url(),
                content=request,
                headers=self._headers(),
            ) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._bound.settings.policy.max_response_bytes:
                        raise RealProviderFailure(
                            "provider_response_too_large",
                            diagnostic=(
                                ProviderDispatchDiagnostic.DISPATCHED_OUTCOME_UNKNOWN
                            ),
                        )
                    chunks.append(chunk)
                return b"".join(chunks), response.status_code
        except RealProviderFailure:
            raise
        except Exception:
            failure = RealProviderFailure(
                "provider_transport_failed",
                diagnostic=ProviderDispatchDiagnostic.DISPATCHED_OUTCOME_UNKNOWN,
                retryable=True,
            )
        if failure is not None:
            raise failure
        raise AssertionError("provider send exited without a result")

    def _read_sync(self, response: _SyncResponse) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self._bound.settings.policy.max_response_bytes:
                raise RealProviderFailure(
                    "provider_response_too_large",
                    diagnostic=ProviderDispatchDiagnostic.DISPATCHED_OUTCOME_UNKNOWN,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _request_bytes(self, messages: tuple[dict[str, str], ...]) -> bytes:
        policy = self._bound.settings.policy
        payload: dict[str, object] = {
            "model": self._bound.settings.model_id,
            "messages": list(messages),
            "stream": False,
            "response_format": {"type": "json_object"},
            "max_tokens": policy.max_output_tokens,
            "thinking": {
                "type": policy.provider_request_policy.thinking_mode.value
            },
            "reasoning_effort": (
                policy.provider_request_policy.reasoning_effort.value
            ),
        }
        if (
            policy.provider_request_policy.sampling_control_mode
            is SamplingControlMode.EFFECTIVE
        ):
            assert policy.temperature is not None and policy.top_p is not None
            payload["temperature"] = float(policy.temperature)
            payload["top_p"] = float(policy.top_p)
        if policy.seed is not None:
            payload["seed"] = policy.seed
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > policy.max_request_bytes:
            raise RealProviderFailure(
                "provider_request_too_large",
                diagnostic=ProviderDispatchDiagnostic.NOT_DISPATCHED,
            )
        # HTTP byte size is only a transport bound and is not treated as a
        # tokenizer or provider-call admission contract. Admission uses the
        # separately frozen ProviderCallReservation.
        return encoded

    def _parse(
        self,
        raw: bytes,
        status_code: int,
        started_ns: int,
    ) -> ChatTransportResponse:
        if not 200 <= status_code < 300:
            retryable = status_code == 429 or 500 <= status_code <= 599
            raise RealProviderFailure(
                (
                    "provider_http_transient"
                    if retryable
                    else "provider_http_permanent"
                ),
                diagnostic=ProviderDispatchDiagnostic.RESPONSE_RECEIVED,
                retryable=retryable,
                http_status=status_code,
            )
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise ValueError
            model_id = envelope["model"]
            if model_id != self._bound.settings.model_id:
                raise ValueError
            choices = envelope["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError
            finish_reason = choice["finish_reason"]
            if not isinstance(finish_reason, str):
                raise ValueError
            usage = envelope["usage"]
            input_tokens = usage["prompt_tokens"]
            output_tokens = usage["completion_tokens"]
            total_tokens = usage["total_tokens"]
            cache_hit_tokens = usage.get("prompt_cache_hit_tokens")
            cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
            if (
                not isinstance(input_tokens, int)
                or isinstance(input_tokens, bool)
                or not isinstance(output_tokens, int)
                or isinstance(output_tokens, bool)
                or not isinstance(total_tokens, int)
                or isinstance(total_tokens, bool)
                or min(input_tokens, output_tokens, total_tokens) < 0
                or total_tokens != input_tokens + output_tokens
            ):
                raise ValueError
            if (
                cache_hit_tokens is not None or cache_miss_tokens is not None
            ) and (
                    not isinstance(cache_hit_tokens, int)
                    or isinstance(cache_hit_tokens, bool)
                    or not isinstance(cache_miss_tokens, int)
                    or isinstance(cache_miss_tokens, bool)
                    or min(cache_hit_tokens, cache_miss_tokens) < 0
                    or cache_hit_tokens + cache_miss_tokens != input_tokens
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise RealProviderFailure(
                "provider_response_invalid",
                diagnostic=ProviderDispatchDiagnostic.RESPONSE_RECEIVED,
            ) from None
        policy = self._bound.settings.policy
        cost = (
            input_tokens
            * policy.input_cost_upper_bound_microunits_per_million_tokens
            + output_tokens
            * policy.output_cost_upper_bound_microunits_per_million_tokens
            + 999_999
        ) // 1_000_000
        measured_usage = RuntimeResourceAmount(
            duration_milliseconds=max(
                0, (time.perf_counter_ns() - started_ns) // 1_000_000
            ),
            tokens=total_tokens,
            cost_microunits=cost,
        )
        failure_codes = {
            "length": ("provider_response_incomplete", False),
            "content_filter": ("provider_response_filtered", False),
            "tool_calls": ("provider_unexpected_tool_call", False),
            "insufficient_system_resource": (
                "provider_resource_unavailable",
                True,
            ),
        }
        if finish_reason != "stop":
            code, retryable = failure_codes.get(
                finish_reason, ("provider_response_invalid", False)
            )
            raise RealProviderFailure(
                code,
                diagnostic=ProviderDispatchDiagnostic.RESPONSE_RECEIVED,
                retryable=retryable,
                usage=measured_usage,
                usage_certainty=UsageCertainty.UPPER_BOUND,
            )
        if (
            input_tokens > policy.max_input_tokens
            or output_tokens > policy.max_output_tokens
            or total_tokens > policy.max_input_tokens + policy.max_output_tokens
        ):
            raise RealProviderFailure(
                "provider_usage_limit_exceeded",
                diagnostic=ProviderDispatchDiagnostic.RESPONSE_RECEIVED,
                usage=measured_usage,
                usage_certainty=UsageCertainty.UPPER_BOUND,
            )
        if cost > policy.max_cost_microunits_per_call:
            raise RealProviderFailure(
                "provider_cost_limit_exceeded",
                diagnostic=ProviderDispatchDiagnostic.RESPONSE_RECEIVED,
                usage=measured_usage,
                usage_certainty=UsageCertainty.UPPER_BOUND,
            )
        try:
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise RealProviderFailure(
                "provider_response_invalid",
                diagnostic=ProviderDispatchDiagnostic.RESPONSE_RECEIVED,
                usage=measured_usage,
                usage_certainty=UsageCertainty.UPPER_BOUND,
            ) from None
        return ChatTransportResponse(
            content=content.encode(),
            model_id=model_id,
            usage=measured_usage,
            usage_certainty=UsageCertainty.UPPER_BOUND,
        )

    def _url(self) -> str:
        return f"{self._bound.settings.base_endpoint.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {self._bound.credential}",
        }
