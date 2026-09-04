"""One-RPC, bounded Zilliz Cloud Free BM25 retrieval adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable
from contextlib import suppress
from time import monotonic
from typing import Any

from researchos.application.errors import MissingOptionalDependency
from researchos.application.real_composition import (
    BoundRealCapability,
    RealCompositionManager,
)
from researchos.application.real_tool_dispatch import RealToolDispatchAuthorizer
from researchos.domain.real_tools import AuthorizedToolDispatchEnvelope
from researchos.domain.retrieval import canonicalize_zilliz_free_or_serverless_endpoint
from researchos.domain.runtime import UsageCertainty
from researchos.domain.tools import (
    LocalRetrievalHit,
    LocalRetrievalRequest,
    LocalRetrievalResult,
    ToolError,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolUsage,
)
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.retrieval import ZillizBm25Transport
from researchos.interfaces.runtime import CancellationSignal


class PymilvusZillizBm25Transport:
    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory

    def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        client_factory = self._client_factory
        if client_factory is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:  # pragma: no cover - dependency boundary
                raise MissingOptionalDependency("retrieval") from exc
            client_factory = MilvusClient
        client = client_factory(uri=kwargs["endpoint"], token=kwargs["credential"])
        try:
            response = client.search(
                collection_name=kwargs["collection_id"],
                data=[kwargs["query"]],
                anns_field=kwargs["anns_field"],
                limit=kwargs["limit"],
                output_fields=list(kwargs["output_fields"]),
                timeout=kwargs["timeout_seconds"],
            )
            if (
                not isinstance(response, list)
                or len(response) != 1
                or not isinstance(response[0], list)
            ):
                raise ValueError("invalid Zilliz search response")
            return response[0]
        finally:
            # A timed-out/cancelled asyncio waiter cannot kill a synchronous
            # RPC thread. The frozen RPC timeout bounds it; this finally closes
            # the client once that thread returns on every terminal path.
            with suppress(Exception):
                client.close()


class ZillizBm25RetrievalTool:
    def __init__(
        self,
        *,
        bound: BoundRealCapability,
        authorizer: RealToolDispatchAuthorizer,
        compositions: RealCompositionManager,
        transport: ZillizBm25Transport,
        clock: Clock,
    ) -> None:
        if bound.settings.retrieval_policy is None:
            raise ValueError("Zilliz retrieval Tool requires retrieval policy")
        self._bound, self._authorizer, self._compositions = (
            bound,
            authorizer,
            compositions,
        )
        self._transport, self._clock = transport, clock

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
        self, envelope: AuthorizedToolDispatchEnvelope, cancellation: CancellationSignal
    ) -> ToolInvocationResult:
        started = monotonic()
        request = envelope.invocation.input
        policy = self._bound.settings.retrieval_policy
        assert policy is not None
        if not isinstance(request, LocalRetrievalRequest):
            return self._failure(
                "retrieval_input_invalid", started, UsageCertainty.EXACT
            )
        if cancellation.cancelled:
            return self._terminal(
                "retrieval_cancelled",
                started,
                ToolInvocationStatus.CANCELLED,
                UsageCertainty.UNKNOWN,
            )
        if (
            len(request.query.encode("utf-8")) > policy.max_query_bytes
            or request.limit > policy.max_results
        ):
            return self._failure(
                "retrieval_request_bound_exceeded", started, UsageCertainty.EXACT
            )
        try:
            self._authorizer.authorize(envelope)
            endpoint = canonicalize_zilliz_free_or_serverless_endpoint(policy.endpoint)
        except Exception:
            return self._failure(
                "real_tool_dispatch_unauthorized", started, UsageCertainty.EXACT
            )
        credential = self._compositions.capability_secret("managed_retrieval")
        if not isinstance(credential, str) or not credential.strip():
            return self._failure(
                "retrieval_credential_missing", started, UsageCertainty.EXACT
            )
        fields = (
            policy.collection_schema.document_id_field,
            policy.collection_schema.chunk_id_field,
            policy.collection_schema.locator_field,
            policy.collection_schema.content_field,
            policy.collection_schema.content_hash_field,
            policy.collection_schema.metadata_field,
        )
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._transport.search,
                endpoint=endpoint,
                credential=credential,
                collection_id=policy.collection_id,
                anns_field=policy.collection_schema.anns_field,
                query=request.query,
                limit=request.limit,
                output_fields=fields,
                timeout_seconds=policy.total_timeout_ms / 1000,
            )
        )
        cancelled = asyncio.create_task(cancellation.wait())
        timeout = asyncio.create_task(
            asyncio.sleep(
                min(
                    policy.total_timeout_ms / 1000,
                    max(
                        0,
                        (
                            envelope.invocation.deadline - self._clock.now()
                        ).total_seconds(),
                    ),
                )
            )
        )
        try:
            done, _ = await asyncio.wait(
                {worker, cancelled, timeout}, return_when=asyncio.FIRST_COMPLETED
            )
            if worker not in done:
                worker.cancel()
                if cancelled in done:
                    return self._terminal(
                        "retrieval_cancelled",
                        started,
                        ToolInvocationStatus.CANCELLED,
                        UsageCertainty.UNKNOWN,
                    )
                return self._terminal(
                    "retrieval_timed_out",
                    started,
                    ToolInvocationStatus.TIMED_OUT,
                    UsageCertainty.UNKNOWN,
                )
            try:
                raw = worker.result()
            except MissingOptionalDependency:
                raise
            except Exception:
                return self._failure(
                    "retrieval_provider_unavailable", started, UsageCertainty.UNKNOWN
                )
        except asyncio.CancelledError:
            # Cancelling the asyncio task detaches only the waiter; the
            # synchronous RPC remains bounded by policy and its transport
            # finally closes the client once it finishes.
            worker.cancel()
            return self._terminal(
                "retrieval_cancelled",
                started,
                ToolInvocationStatus.CANCELLED,
                UsageCertainty.UNKNOWN,
            )
        finally:
            for task in (cancelled, timeout):
                task.cancel()
            for task in (cancelled, timeout):
                with suppress(asyncio.CancelledError):
                    await task
        try:
            hits = self._validate_hits(raw, policy, request.limit)
        except Exception:
            return self._failure(
                "retrieval_response_invalid", started, UsageCertainty.UPPER_BOUND
            )
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=LocalRetrievalResult(
                adapter_id=self.descriptor.adapter_id,
                retrieved_at=self._clock.now(),
                hits=hits,
                matched_count=len(hits),
            ),
            usage=self._usage(started),
            usage_certainty=UsageCertainty.UPPER_BOUND,
        )

    def _validate_hits(
        self, raw: object, policy, limit: int
    ) -> tuple[LocalRetrievalHit, ...]:
        if not isinstance(raw, list) or len(raw) > limit:
            raise ValueError
        fields = policy.collection_schema
        hits = []
        seen = set()
        for item in raw:
            entity = item.get("entity", item) if isinstance(item, dict) else None
            if not isinstance(entity, dict):
                raise ValueError
            content = entity[fields.content_field]
            stored_hash = entity[fields.content_hash_field]
            metadata = entity[fields.metadata_field]
            if (
                not isinstance(content, str)
                or not isinstance(stored_hash, str)
                or hashlib.sha256(content.encode("utf-8")).hexdigest() != stored_hash
                or len(content.encode("utf-8")) > policy.max_content_bytes
            ):
                raise ValueError
            encoded_metadata = json.dumps(
                metadata, ensure_ascii=False, allow_nan=False, sort_keys=True
            ).encode("utf-8")
            if (
                not isinstance(metadata, dict)
                or len(encoded_metadata) > policy.max_metadata_bytes
            ):
                raise ValueError
            score = item.get("distance", item.get("score", 0))
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                or score < 0
            ):
                raise ValueError
            identity = (entity[fields.document_id_field], entity[fields.chunk_id_field])
            if identity in seen:
                raise ValueError
            seen.add(identity)
            hits.append(
                LocalRetrievalHit(
                    document_id=identity[0],
                    chunk_id=identity[1],
                    locator=entity[fields.locator_field],
                    content=content,
                    content_hash=stored_hash,
                    score=float(score),
                    source_metadata=metadata,
                )
            )
        return tuple(hits)

    def _usage(self, started: float) -> ToolUsage:
        return ToolUsage(
            duration_milliseconds=max(0, int((monotonic() - started) * 1000)),
            tool_calls=1,
        )

    def _failure(
        self, code: str, started: float, certainty: UsageCertainty
    ) -> ToolInvocationResult:
        return self._terminal(code, started, ToolInvocationStatus.FAILED, certainty)

    def _terminal(
        self,
        code: str,
        started: float,
        status: ToolInvocationStatus,
        certainty: UsageCertainty,
    ) -> ToolInvocationResult:
        usage = None if certainty is UsageCertainty.UNKNOWN else self._usage(started)
        return ToolInvocationResult(
            status=status,
            error=ToolError(
                code=code,
                message="Zilliz retrieval failed",
                retryable=code == "retrieval_provider_unavailable",
            ),
            usage=usage,
            usage_certainty=certainty,
        )
