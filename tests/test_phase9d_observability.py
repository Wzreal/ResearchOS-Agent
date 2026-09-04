from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from researchos.adapters.memory import InMemoryRunStore, InMemoryTraceSink
from researchos.adapters.mock_observability import MockObservationExporter
from researchos.application.observability_runtime import ObservabilityRuntime
from researchos.application.observation_projection import project_trace_event
from researchos.application.observation_recorder import ExporterDispatcher
from researchos.application.run_manager import RunManager
from researchos.configuration.environment import EnvironmentSecretSource
from researchos.configuration.observability import OtlpHttpSettings
from researchos.domain.contracts import RunConfig, RunInput, TraceEvent, TraceEventType
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
