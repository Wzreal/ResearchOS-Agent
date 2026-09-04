from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from researchos.adapters.memory import InMemoryRunStore, InMemoryTraceSink
from researchos.adapters.mock_observability import MockObservationExporter
from researchos.application.observability_runtime import (
    ObservabilityRuntime,
    build_optional_observability_runtime,
)
from researchos.application.observation_projection import project_trace_event
from researchos.application.observation_recorder import ExporterDispatcher
from researchos.application.run_manager import RunManager
from researchos.configuration.environment import EnvironmentSecretSource
from researchos.configuration.observability import (
    OtlpHttpSettings,
    load_otlp_http_settings,
)
from researchos.domain.contracts import (
    BudgetUsage,
    ErrorCategory,
    RunConfig,
    RunError,
    RunInput,
    TraceEvent,
    TraceEventType,
)
from researchos.domain.observability import (
    AppendOnceResult,
    ExporterPolicy,
    ObservationScope,
)
from researchos.domain.runtime import TraceEventDescriptor


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 9, 4, tzinfo=UTC)


def _event(event_id: str = "evt_observe") -> TraceEvent:
    return TraceEvent(
        event_id=event_id,
        event_type=TraceEventType.TOOL_INVOCATION_STARTED,
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
        run_id="run_observe",
        revision=1,
        correlation_id="attempt_observe",
        attributes={"task_id": "task_observe", "tool_call_id": "call_observe"},
    )


def test_sync_trace_sink_mirrors_without_caller_event_loop() -> None:
    exporter = MockObservationExporter()
    runtime = ObservabilityRuntime(
        local_trace=InMemoryTraceSink(), clock=Clock(), exporters=(exporter,)
    )
    runtime.trace_sink.append(_event())
    for _ in range(100):
        if exporter.calls:
            break
        __import__("time").sleep(0.01)
    assert exporter.calls == 1
    runtime.close_sync()


def test_sync_run_manager_trace_writes_enqueue_without_event_loop() -> None:
    exporter = MockObservationExporter()
    local = InMemoryTraceSink()
    runtime = ObservabilityRuntime(
        local_trace=local, clock=Clock(), exporters=(exporter,)
    )
    ids = iter(("run_observe", "tr_observe", "evt_intent", "evt_committed"))
    manager = RunManager(
        store=InMemoryRunStore(),
        trace_sink=runtime.trace_sink,
        clock=Clock(),
        id_factory=lambda _prefix: next(ids),
    )
    manager.create(RunInput(query="safe query"), RunConfig())
    for _ in range(100):
        if exporter.calls == 2:
            break
        __import__("time").sleep(0.01)
    assert exporter.calls == 2
    assert len(local.read("run_observe")) == 2
    runtime.close_sync()


def test_append_once_already_present_does_not_reexport() -> None:
    exporter = MockObservationExporter()
    runtime = ObservabilityRuntime(
        local_trace=InMemoryTraceSink(), clock=Clock(), exporters=(exporter,)
    )
    descriptor = TraceEventDescriptor.from_event(_event())
    assert runtime.trace_sink.append_once(descriptor) is AppendOnceResult.APPENDED
    assert (
        runtime.trace_sink.append_once(descriptor) is AppendOnceResult.ALREADY_PRESENT
    )
    for _ in range(100):
        if exporter.calls:
            break
        __import__("time").sleep(0.01)
    assert exporter.calls == 1
    runtime.close_sync()


def test_concurrent_sync_offers_are_bounded_and_thread_safe() -> None:
    exporter = MockObservationExporter(delay_seconds=1)
    dispatcher = ExporterDispatcher(
        exporters=(exporter,),
        policy=ExporterPolicy(max_pending_deliveries=2, max_concurrency=1),
    )
    envelope = project_trace_event(_event())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: dispatcher.offer(envelope), range(20)))
    assert sum(item.value == "accepted" for item in results) <= 2
    dispatcher.close_sync()


def test_projection_uses_fixed_scope_and_identity_sources() -> None:
    projected = project_trace_event(_event())
    assert projected.scope is ObservationScope.TOOL
    assert projected.task_id == "task_observe"
    assert projected.attempt_id == "attempt_observe"
    assert projected.tool_call_id == "call_observe"
    local = project_trace_event(
        _event("evt_local").model_copy(
            update={"event_type": TraceEventType.OBSERVABILITY_EXPORT_FAILED}
        )
    )
    assert local.scope is ObservationScope.OBSERVABILITY
    assert local.export is False


def test_disabled_runtime_creates_no_dispatcher_or_worker() -> None:
    runtime = ObservabilityRuntime(local_trace=InMemoryTraceSink(), clock=Clock())
    assert runtime.enabled is False
    assert runtime.dispatcher is None


def test_value_only_otlp_auth_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="required together"):
        load_otlp_http_settings(
            {
                "RESEARCHOS_OTLP_TRACES_ENDPOINT": "https://collector.example/v1/traces",
                "RESEARCHOS_OTLP_AUTH_HEADER_VALUE": "secret",
            }
        )


class _LoopRecordingExporter:
    exporter_id = "loop_recording_exporter"

    def __init__(self) -> None:
        self.export_loops: list[int] = []
        self.close_loop: int | None = None

    async def export(self, _envelope) -> None:
        self.export_loops.append(id(asyncio.get_running_loop()))
        await asyncio.sleep(0)

    async def aclose(self) -> None:
        self.close_loop = id(asyncio.get_running_loop())


def test_dispatcher_uses_one_loop_for_concurrent_delivery_and_close() -> None:
    exporter = _LoopRecordingExporter()
    dispatcher = ExporterDispatcher(
        exporters=(exporter,),
        policy=ExporterPolicy(max_pending_deliveries=8, max_concurrency=4),
    )
    for index in range(4):
        assert (
            dispatcher.offer(project_trace_event(_event(f"evt_loop_{index}"))).value
            == "accepted"
        )
    dispatcher.close_sync(drain=True)
    assert dispatcher.worker_count == 1
    assert len(exporter.export_loops) == 4
    assert set(exporter.export_loops) == {exporter.close_loop}


@pytest.mark.requires_real_extra
def test_env_settings_factory_enables_real_otlp_runtime() -> None:
    settings = load_otlp_http_settings(
        {
            "RESEARCHOS_OTLP_TRACES_ENDPOINT": "https://collector.example/v1/traces",
            "RESEARCHOS_OTLP_AUTH_HEADER_NAME": "Authorization",
            "RESEARCHOS_OTLP_AUTH_HEADER_VALUE": "canary-not-persisted",
        }
    )
    runtime = build_optional_observability_runtime(
        local_trace=InMemoryTraceSink(),
        clock=Clock(),
        settings=settings,
        secrets=EnvironmentSecretSource(
            {"researchos_otlp_auth_header_value": "canary-not-persisted"}
        ),
    )
    assert runtime.enabled is True
    assert runtime.dispatcher is not None
    runtime.close_sync()


def test_otlp_settings_reject_forbidden_headers_before_http() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        OtlpHttpSettings(
            traces_endpoint="https://collector.example/v1/traces",
            auth_header_name="Host",
            auth_credential_slot_id="researchos_otlp_auth_header_value",
        )


@pytest.mark.requires_real_extra
def test_otlp_client_disables_redirects_and_environment_and_hides_secret() -> None:
    from researchos.adapters.otlp_observability import OtlpHttpObservationExporter

    canary = "secret-canary-not-exported"
    exporter = OtlpHttpObservationExporter(
        settings=OtlpHttpSettings(
            traces_endpoint="https://collector.example/v1/traces",
            auth_header_name="Authorization",
            auth_credential_slot_id="researchos_otlp_auth_header_value",
        ),
        secrets=EnvironmentSecretSource({"researchos_otlp_auth_header_value": canary}),
    )
    assert exporter._client._trust_env is False
    assert exporter._client.follow_redirects is False
    payload = exporter._payload(project_trace_event(_event()))
    assert canary.encode() not in payload
    assert canary not in repr(exporter._settings)
    asyncio.run(exporter.aclose())


class _OfflineOtlpResponse:
    status_code = 202

    async def aiter_bytes(self):
        if False:  # pragma: no cover - makes this an async generator
            yield b""


class _OfflineOtlpStream:
    def __init__(self, response: _OfflineOtlpResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _OfflineOtlpResponse:
        return self._response

    async def __aexit__(self, *_args: object) -> None:
        return None


class _OfflineOtlpClient:
    def __init__(self, *, status_code: int = 202) -> None:
        self.payloads: list[bytes] = []
        self.export_loops: list[int] = []
        self.close_loop: int | None = None
        self._status_code = status_code

    def stream(self, _method: str, _url: str, *, content: bytes, **_kwargs: object):
        self.payloads.append(content)
        self.export_loops.append(id(asyncio.get_running_loop()))
        response = _OfflineOtlpResponse()
        response.status_code = self._status_code
        return _OfflineOtlpStream(response)

    async def aclose(self) -> None:
        self.close_loop = id(asyncio.get_running_loop())


@pytest.mark.requires_real_extra
def test_real_otlp_adapter_runs_in_dispatcher_loop_offline() -> None:
    from researchos.adapters.otlp_observability import OtlpHttpObservationExporter

    client = _OfflineOtlpClient()
    exporter = OtlpHttpObservationExporter(
        settings=OtlpHttpSettings(
            traces_endpoint="https://collector.example/v1/traces"
        ),
        secrets=EnvironmentSecretSource(),
        client_factory=lambda **_kwargs: client,
    )
    local = InMemoryTraceSink()
    runtime = ObservabilityRuntime(
        local_trace=local,
        clock=Clock(),
        exporters=(exporter,),
        policy=ExporterPolicy(max_pending_deliveries=4, max_concurrency=2),
    )
    runtime.trace_sink.append(_event("evt_otlp_a"))
    runtime.trace_sink.append(_event("evt_otlp_b"))
    runtime.close_sync(drain=True)
    assert len(local.read("run_observe")) == 2
    assert len(client.payloads) == 2
    assert set(client.export_loops) == {client.close_loop}


@pytest.mark.requires_real_extra
def test_otlp_remote_failure_does_not_change_canonical_trace() -> None:
    from researchos.adapters.otlp_observability import OtlpHttpObservationExporter

    client = _OfflineOtlpClient(status_code=500)
    exporter = OtlpHttpObservationExporter(
        settings=OtlpHttpSettings(
            traces_endpoint="https://collector.example/v1/traces"
        ),
        secrets=EnvironmentSecretSource(),
        client_factory=lambda **_kwargs: client,
    )
    local = InMemoryTraceSink()
    runtime = ObservabilityRuntime(
        local_trace=local, clock=Clock(), exporters=(exporter,)
    )
    runtime.trace_sink.append(_event("evt_otlp_failure"))
    runtime.close_sync(drain=True)
    assert local.read("run_observe")[0].event_id == "evt_otlp_failure"


@pytest.mark.requires_real_extra
def test_otlp_payload_links_causation_and_exports_fixed_safe_fields() -> None:
    from researchos.adapters.otlp_observability import OtlpHttpObservationExporter

    exporter = OtlpHttpObservationExporter(
        settings=OtlpHttpSettings(
            traces_endpoint="https://collector.example/v1/traces"
        ),
        secrets=EnvironmentSecretSource(),
    )
    parent = _event("evt_parent")
    child = parent.model_copy(
        update={
            "event_id": "evt_child",
            "causation_id": "evt_parent",
            "transition_id": "tr_observe",
            "duration_ms": 7,
            "budget_delta": BudgetUsage(tokens=3),
            "error": RunError(
                error_id="err_observe",
                code="safe_error",
                category=ErrorCategory.INTERNAL,
                message="secret error prose must not export",
                retryable=True,
                occurred_at=Clock().now(),
                details={"raw": "do-not-export"},
            ),
            "attributes": {"raw_prompt": "do-not-export", "task_id": "task_observe"},
        }
    )
    request_type = exporter._trace_service_pb2.ExportTraceServiceRequest
    decoded_parent = request_type.FromString(
        exporter._payload(project_trace_event(parent))
    )
    decoded_child = request_type.FromString(
        exporter._payload(project_trace_event(child))
    )
    parent_span = decoded_parent.resource_spans[0].scope_spans[0].spans[0]
    child_span = decoded_child.resource_spans[0].scope_spans[0].spans[0]
    assert child_span.parent_span_id == parent_span.span_id
    assert child_span.trace_id == parent_span.trace_id
    values = {item.key: item.value.string_value for item in child_span.attributes}
    assert values["researchos.transition_id"] == "tr_observe"
    assert values["researchos.budget_delta.tokens"] == "3"
    assert values["researchos.error.code"] == "safe_error"
    assert "raw_prompt" not in values
    assert all("do-not-export" not in value for value in values.values())
    assert (
        decoded_child.resource_spans[0].resource.attributes[0].value.string_value
        == "researchos-agent"
    )
    asyncio.run(exporter.aclose())
