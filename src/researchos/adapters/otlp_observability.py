"""Minimal optional OTLP/HTTP protobuf exporter without an SDK."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC
from typing import Any

from researchos.application.errors import MissingOptionalDependency
from researchos.configuration.observability import OtlpHttpSettings
from researchos.domain.observability import ObservationEnvelope
from researchos.interfaces.providers import SecretSource


class OtlpHttpObservationExporter:
    """Exports one safe envelope as one OTLP span; no retry or redirect."""

    def __init__(
        self,
        *,
        settings: OtlpHttpSettings,
        secrets: SecretSource,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        try:
            import httpx
            from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
            from opentelemetry.proto.common.v1 import common_pb2
            from opentelemetry.proto.resource.v1 import resource_pb2
            from opentelemetry.proto.trace.v1 import trace_pb2
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise MissingOptionalDependency("observability extra is required") from exc
        self._httpx = httpx
        self._trace_service_pb2 = trace_service_pb2
        self._common_pb2 = common_pb2
        self._resource_pb2 = resource_pb2
        self._trace_pb2 = trace_pb2
        self._settings = settings
        self.exporter_id = settings.exporter_id
        headers = {
            "content-type": "application/x-protobuf",
            "accept": "application/x-protobuf",
        }
        if settings.auth_header_name is not None:
            value = secrets.get_secret(settings.auth_credential_slot_id or "")
            self._validate_secret(value)
            headers[settings.auth_header_name] = value
        self._headers = headers
        self._client = (client_factory or httpx.AsyncClient)(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_ms / 1000,
                read=settings.read_timeout_ms / 1000,
                write=settings.write_timeout_ms / 1000,
                pool=settings.pool_timeout_ms / 1000,
            ),
        )

    @staticmethod
    def _validate_secret(value: str | None) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 8_192
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("OTLP auth credential is invalid")

    @staticmethod
    def _digest(value: str, length: int) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()[:length]

    @classmethod
    def _span_id(cls, event_id: str) -> bytes:
        return cls._digest("span:" + event_id, 8)

    @classmethod
    def _trace_id(cls, run_id: str) -> bytes:
        return cls._digest("trace:" + run_id, 16)

    def _payload(self, envelope: ObservationEnvelope) -> bytes:
        descriptor = envelope.descriptor
        timestamp = descriptor.timestamp.astimezone(UTC)
        nanos = int(timestamp.timestamp() * 1_000_000_000)
        attrs = {
            "researchos.event_id": descriptor.event_id,
            "researchos.event_hash": descriptor.canonical_event_hash,
            "researchos.run_id": descriptor.run_id,
            "researchos.revision": str(descriptor.revision),
            "researchos.scope": envelope.scope.value,
        }
        fixed = {
            "transition_id": descriptor.transition_id,
            "correlation_id": descriptor.correlation_id,
            "causation_id": descriptor.causation_id,
            "previous_status": (
                descriptor.previous_status.value
                if descriptor.previous_status is not None
                else None
            ),
            "next_status": (
                descriptor.next_status.value
                if descriptor.next_status is not None
                else None
            ),
            "duration_ms": descriptor.duration_ms,
        }
        for key, value in fixed.items():
            if value is not None:
                attrs[f"researchos.{key}"] = str(value)
        if descriptor.budget_delta is not None:
            for key in (
                "elapsed_milliseconds",
                "tokens",
                "cost_microunits",
                "tool_calls",
            ):
                attrs[f"researchos.budget_delta.{key}"] = str(
                    getattr(descriptor.budget_delta, key)
                )
        if descriptor.error is not None:
            attrs["researchos.error.code"] = descriptor.error.code
            attrs["researchos.error.category"] = descriptor.error.category.value
            attrs["researchos.error.retryable"] = str(
                descriptor.error.retryable
            ).lower()
        for key in (
            "task_id",
            "attempt_id",
            "agent_id",
            "tool_call_id",
            "evidence_id",
            "claim_id",
            "verification_id",
            "evaluation_id",
        ):
            value = getattr(envelope, key)
            if value is not None:
                attrs[f"researchos.{key}"] = value
        key_values = [
            self._common_pb2.KeyValue(
                key=key, value=self._common_pb2.AnyValue(string_value=value)
            )
            for key, value in sorted(attrs.items())
        ]
        span = self._trace_pb2.Span(
            trace_id=self._trace_id(descriptor.run_id),
            span_id=self._span_id(descriptor.event_id),
            parent_span_id=(
                self._span_id(descriptor.causation_id)
                if descriptor.causation_id
                else b""
            ),
            name=descriptor.event_type.value,
            start_time_unix_nano=nanos,
            end_time_unix_nano=nanos,
            kind=self._trace_pb2.Span.SPAN_KIND_INTERNAL,
            attributes=key_values,
        )
        request = self._trace_service_pb2.ExportTraceServiceRequest(
            resource_spans=[
                self._trace_pb2.ResourceSpans(
                    resource=self._resource_pb2.Resource(
                        attributes=[
                            self._common_pb2.KeyValue(
                                key="service.name",
                                value=self._common_pb2.AnyValue(
                                    string_value="researchos-agent"
                                ),
                            ),
                            self._common_pb2.KeyValue(
                                key="researchos.exporter.schema_version",
                                value=self._common_pb2.AnyValue(string_value="v1"),
                            ),
                        ]
                    ),
                    scope_spans=[self._trace_pb2.ScopeSpans(spans=[span])],
                )
            ]
        )
        encoded = request.SerializeToString(deterministic=True)
        if len(encoded) > self._settings.max_request_bytes:
            raise ValueError("OTLP request exceeds configured byte bound")
        return encoded

    async def export(self, envelope: ObservationEnvelope) -> None:
        async with asyncio.timeout(self._settings.total_timeout_ms / 1_000):
            payload = self._payload(envelope)
            async with self._client.stream(
                "POST",
                self._settings.traces_endpoint,
                content=payload,
                headers=self._headers,
            ) as response:
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._settings.max_response_bytes:
                        raise ValueError("OTLP response exceeds configured byte bound")
                if response.status_code not in {200, 202}:
                    raise RuntimeError("OTLP export failed")

    async def aclose(self) -> None:
        await self._client.aclose()
