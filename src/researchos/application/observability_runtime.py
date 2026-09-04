"""Optional full-trace observability runtime with local-first semantics."""

from __future__ import annotations

from researchos.application.observation_projection import project_trace_event
from researchos.application.observation_recorder import (
    ExporterDispatcher,
    ObservationRecorder,
)
from researchos.configuration.observability import OtlpHttpSettings
from researchos.domain.contracts import TraceEvent
from researchos.domain.observability import AppendOnceResult, ExporterPolicy
from researchos.domain.runtime import TraceEventDescriptor
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.interfaces.providers import SecretSource
from researchos.interfaces.verification import ObservationExporter


class ObservabilityTraceSink:
    """Synchronous TraceSink decorator: append local first, then offer once."""

    def __init__(
        self, *, local_trace: TraceSink, recorder: ObservationRecorder
    ) -> None:
        self._local = local_trace
        self._recorder = recorder

    def append(self, event: TraceEvent) -> None:
        self._local.append(event)
        self._recorder.offer_persisted(project_trace_event(event))

    def append_once(self, descriptor: TraceEventDescriptor) -> AppendOnceResult:
        result = self._local.append_once(descriptor)
        if result is AppendOnceResult.APPENDED:
            self._recorder.offer_persisted(project_trace_event(descriptor))
        return result

    def read(
        self, run_id: str, *, recover_torn_tail: bool = False
    ) -> tuple[TraceEvent, ...]:
        return self._local.read(run_id, recover_torn_tail=recover_torn_tail)


class ObservabilityRuntime:
    """Explicit owner of optional dispatcher lifetime; disabled owns no thread."""

    def __init__(
        self,
        *,
        local_trace: TraceSink,
        clock: Clock,
        exporters: tuple[ObservationExporter, ...] = (),
        policy: ExporterPolicy | None = None,
    ) -> None:
        self.dispatcher = (
            ExporterDispatcher(exporters=exporters, policy=policy)
            if exporters
            else None
        )
        self.recorder = ObservationRecorder(
            trace_sink=local_trace, clock=clock, dispatcher=self.dispatcher
        )
        self.trace_sink: TraceSink = (
            ObservabilityTraceSink(local_trace=local_trace, recorder=self.recorder)
            if self.dispatcher is not None
            else local_trace
        )

    @property
    def enabled(self) -> bool:
        return self.dispatcher is not None

    def close_sync(self, *, drain: bool = False) -> None:
        if self.dispatcher is not None:
            self.dispatcher.close_sync(drain=drain)

    async def close(self, *, drain: bool = False) -> None:
        if self.dispatcher is not None:
            await self.dispatcher.close(drain=drain)


def build_optional_observability_runtime(
    *,
    local_trace: TraceSink,
    clock: Clock,
    settings: OtlpHttpSettings | None,
    secrets: SecretSource,
    policy: ExporterPolicy | None = None,
) -> ObservabilityRuntime:
    """Wire optional environment settings without adding a trace authority."""

    if settings is None:
        return ObservabilityRuntime(local_trace=local_trace, clock=clock, policy=policy)
    # Import only when configured, preserving core/disabled optional isolation.
    from researchos.adapters.otlp_observability import OtlpHttpObservationExporter

    return ObservabilityRuntime(
        local_trace=local_trace,
        clock=clock,
        exporters=(OtlpHttpObservationExporter(settings=settings, secrets=secrets),),
        policy=policy,
    )
