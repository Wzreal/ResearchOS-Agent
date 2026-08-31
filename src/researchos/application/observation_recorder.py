"""Local-first observations and isolated optional exporter dispatch."""

from __future__ import annotations

import asyncio
import queue
from collections.abc import Callable, Iterable
from contextlib import suppress
from threading import Event, Thread

from researchos.domain.contracts import TraceEvent, TraceEventType
from researchos.domain.identity import stable_id
from researchos.domain.observability import (
    AppendOnceResult,
    ExporterOfferResult,
    ExporterPolicy,
    ObservationEnvelope,
    ObservationScope,
)
from researchos.domain.runtime import TraceEventDescriptor
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.interfaces.verification import ObservationExporter
from researchos.security.redaction import PersistenceRedactor

FailureCallback = Callable[[ObservationEnvelope, str, str], None]


class _DiagnosticDispatcher:
    def __init__(self, *, max_pending: int, max_concurrency: int) -> None:
        self._queue: queue.Queue[
            tuple[FailureCallback, ObservationEnvelope, str, str]
        ] = queue.Queue(maxsize=max_pending)
        self._stopped = Event()
        self._workers = tuple(
            Thread(
                target=self._worker,
                name=f"researchos-export-diagnostic-{index}",
                daemon=True,
            )
            for index in range(max_concurrency)
        )
        for worker in self._workers:
            worker.start()

    def offer(
        self,
        callback: FailureCallback,
        envelope: ObservationEnvelope,
        exporter_id: str,
        code: str,
    ) -> None:
        if self._stopped.is_set():
            return
        with suppress(queue.Full):
            self._queue.put_nowait((callback, envelope, exporter_id, code))

    def close(self) -> None:
        self._stopped.set()

    def _worker(self) -> None:
        while not self._stopped.is_set():
            try:
                callback, envelope, exporter_id, code = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with suppress(Exception):
                    callback(envelope, exporter_id, code)
            finally:
                self._queue.task_done()


class ExporterDispatcher:
    """Non-durable remote delivery workers; ``offer`` never performs I/O."""

    def __init__(
        self,
        *,
        exporters: Iterable[ObservationExporter] = (),
        policy: ExporterPolicy | None = None,
        failure_callback: FailureCallback | None = None,
    ) -> None:
        self._exporters = tuple(exporters)
        self._policy = policy or ExporterPolicy()
        exporter_ids = [item.exporter_id for item in self._exporters]
        if len(exporter_ids) != len(set(exporter_ids)):
            raise ValueError("exporter IDs must be unique")
        self._queue: asyncio.Queue[tuple[ObservationExporter, ObservationEnvelope]] = (
            asyncio.Queue(maxsize=self._policy.max_pending_deliveries)
        )
        self._failure_callback = failure_callback
        self._diagnostics = _DiagnosticDispatcher(
            max_pending=self._policy.max_pending_diagnostics,
            max_concurrency=self._policy.max_diagnostic_concurrency,
        )
        self._workers: list[asyncio.Task[None]] = []
        self._started = False
        self._stopped = False

    def start(self) -> None:
        if self._started or self._stopped or not self._exporters:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._started = True
        self._workers = [
            loop.create_task(self._worker(), name=f"researchos-exporter-{index}")
            for index in range(self._policy.max_concurrency)
        ]

    def set_failure_callback(
        self, callback: FailureCallback
    ) -> None:
        self._failure_callback = callback

    def offer(self, envelope: ObservationEnvelope) -> ExporterOfferResult:
        if not envelope.export or not self._exporters:
            return ExporterOfferResult.ACCEPTED
        if self._stopped:
            return ExporterOfferResult.DISPATCHER_STOPPED
        if not self._started:
            self.start()
        if not self._started:
            return ExporterOfferResult.DISPATCHER_STOPPED
        required = len(self._exporters)
        if self._queue.qsize() + required > self._policy.max_pending_deliveries:
            return ExporterOfferResult.DROPPED_QUEUE_FULL
        try:
            for exporter in self._exporters:
                self._queue.put_nowait((exporter, envelope))
        except asyncio.QueueFull:
            # The event-loop thread is the sole offer mutation boundary. This
            # branch is defensive; DROP_NEW remains deterministic.
            return ExporterOfferResult.DROPPED_QUEUE_FULL
        return ExporterOfferResult.ACCEPTED

    def offer_diagnostic(
        self,
        callback: FailureCallback,
        envelope: ObservationEnvelope,
        exporter_id: str,
        code: str,
    ) -> None:
        """Submit local diagnostic I/O without touching the business loop."""

        self._diagnostics.offer(callback, envelope, exporter_id, code)

    async def close(self, *, drain: bool = False) -> None:
        self._stopped = True
        if drain and self._workers:
            await self._queue.join()
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._diagnostics.close()

    async def _worker(self) -> None:
        while True:
            exporter, envelope = await self._queue.get()
            try:
                await asyncio.wait_for(
                    exporter.export(envelope),
                    timeout=self._policy.export_timeout_ms / 1_000,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._failure_callback is not None:
                    # Filesystem diagnostics execute on a bounded daemon-thread
                    # boundary, never on the asyncio business loop.
                    self._diagnostics.offer(
                        self._failure_callback,
                        envelope,
                        exporter.exporter_id,
                        type(exc).__name__,
                    )
            finally:
                self._queue.task_done()


class ObservationRecorder:
    def __init__(
        self,
        *,
        trace_sink: TraceSink,
        clock: Clock,
        dispatcher: ExporterDispatcher | None = None,
    ) -> None:
        self._trace = trace_sink
        self._clock = clock
        self._dispatcher = dispatcher
        self._redactor = PersistenceRedactor()
        if dispatcher is not None:
            dispatcher.set_failure_callback(self._record_export_failure)

    def record(self, envelope: ObservationEnvelope) -> AppendOnceResult:
        self._redactor.assert_safe_model(envelope)
        result = self._trace.append_once(envelope.descriptor)
        if envelope.export and self._dispatcher is not None:
            offered = self._dispatcher.offer(envelope)
            if offered is not ExporterOfferResult.ACCEPTED:
                # The original local event is already authoritative. A
                # bounded best-effort diagnostic executes off the event loop.
                self._dispatcher.offer_diagnostic(
                    self._record_delivery_dropped,
                    envelope,
                    "exporter_dispatcher",
                    offered.value,
                )
        return result

    def record_descriptor(
        self,
        descriptor: TraceEventDescriptor,
        *,
        scope: ObservationScope = ObservationScope.VERIFICATION,
        export: bool = True,
        verification_id: str | None = None,
    ) -> AppendOnceResult:
        if verification_id is None and scope in {
            ObservationScope.VERIFICATION,
            ObservationScope.RECOVERY,
        }:
            verification_id = descriptor.correlation_id
        return self.record(
            ObservationEnvelope(
                envelope_id=stable_id(
                    "obs", [descriptor.event_id, descriptor.canonical_event_hash]
                ),
                scope=scope,
                descriptor=descriptor,
                export=export,
                verification_id=verification_id,
            )
        )

    def _record_export_failure(
        self, envelope: ObservationEnvelope, exporter_id: str, code: str
    ) -> None:
        self._record_local_diagnostic(
            original=envelope,
            event_type=TraceEventType.OBSERVABILITY_EXPORT_FAILED,
            code=code,
            exporter_id=exporter_id,
        )

    def _record_delivery_dropped(
        self, envelope: ObservationEnvelope, exporter_id: str, code: str
    ) -> None:
        del exporter_id
        self._record_local_diagnostic(
            original=envelope,
            event_type=TraceEventType.OBSERVABILITY_DELIVERY_DROPPED,
            code=code,
        )

    def _record_local_diagnostic(
        self,
        *,
        original: ObservationEnvelope,
        event_type: TraceEventType,
        code: str,
        exporter_id: str | None = None,
    ) -> None:
        source = original.descriptor
        event = TraceEvent(
            event_id=stable_id(
                "evt", [source.event_id, event_type.value, exporter_id, code]
            ),
            event_type=event_type,
            timestamp=self._clock.now(),
            run_id=source.run_id,
            revision=source.revision,
            correlation_id=source.correlation_id,
            causation_id=source.event_id,
            attributes={
                "source_event_id": source.event_id,
                "failure_code": code,
                "exporter_id": exporter_id,
                "export": False,
            },
        )
        descriptor = TraceEventDescriptor.from_event(event)
        # Local-only and deliberately bypasses the dispatcher to prevent recursion.
        self._trace.append_once(descriptor)
