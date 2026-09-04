"""Minimal optional OTLP/HTTP protobuf exporter without an SDK."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC

from researchos.application.errors import MissingOptionalDependency
from researchos.configuration.observability import OtlpHttpSettings
from researchos.domain.observability import ObservationEnvelope
from researchos.interfaces.providers import SecretSource


class OtlpHttpObservationExporter:
    """Exports one safe envelope as one OTLP span; no retry or redirect."""

    def __init__(self, *, settings: OtlpHttpSettings, secrets: SecretSource) -> None:
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
        self._client = httpx.AsyncClient(
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
            trace_id=self._digest("trace:" + descriptor.run_id, 16),
            span_id=self._digest("span:" + descriptor.event_id, 8),
            parent_span_id=(
                self._digest("parent:" + descriptor.causation_id, 8)
                if descriptor.causation_id
                else b""
            ),
            name=descriptor.event_type.value,
            start_time_unix_nano=nanos,
            end_time_unix_nano=nanos,
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
                                    string_value="researchos"
                                ),
                            )
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
