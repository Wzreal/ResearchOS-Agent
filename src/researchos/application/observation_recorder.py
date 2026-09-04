"""Local-first observations and isolated optional exporter dispatch."""

from __future__ import annotations

import asyncio
import queue
from collections.abc import Callable, Iterable
from contextlib import suppress
from threading import Event, Lock, Thread

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
    """Thread-safe, non-durable remote delivery workers.

    The local trace API is synchronous, so exporters deliberately own a
    background execution context.  ``offer`` only reserves a bounded queue
    slot; it never requires a caller event loop or performs network I/O.
    """

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
        self._queue: queue.Queue[tuple[ObservationExporter, ObservationEnvelope]] = (
            queue.Queue()
        )
        self._failure_callback = failure_callback
        self._diagnostics = (
            _DiagnosticDispatcher(
                max_pending=self._policy.max_pending_diagnostics,
                max_concurrency=self._policy.max_diagnostic_concurrency,
            )
            if self._exporters
            else None
        )
        self._workers: list[Thread] = []
        self._pending_lock = Lock()
        self._pending = 0
        self._started = False
        self._stopped = False
        self._accepting = True

    def start(self) -> None:
        if self._started or self._stopped or not self._exporters:
            return
        self._started = True
        self._workers = [
            Thread(
                target=self._worker,
                name=f"researchos-exporter-{index}",
                daemon=True,
            )
            for index in range(self._policy.max_concurrency)
        ]
        for worker in self._workers:
            worker.start()

    def set_failure_callback(self, callback: FailureCallback) -> None:
        self._failure_callback = callback

    def offer(self, envelope: ObservationEnvelope) -> ExporterOfferResult:
        if not envelope.export or not self._exporters:
            return ExporterOfferResult.ACCEPTED
        if self._stopped or not self._accepting:
            return ExporterOfferResult.DISPATCHER_STOPPED
        if not self._started:
            self.start()
        if not self._started:
            return ExporterOfferResult.DISPATCHER_STOPPED
        required = len(self._exporters)
        with self._pending_lock:
            if self._pending + required > self._policy.max_pending_deliveries:
                return ExporterOfferResult.DROPPED_QUEUE_FULL
            self._pending += required
            for exporter in self._exporters:
                self._queue.put_nowait((exporter, envelope))
        return ExporterOfferResult.ACCEPTED

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    @property
    def started(self) -> bool:
        return self._started

    def offer_diagnostic(
        self,
        callback: FailureCallback,
        envelope: ObservationEnvelope,
        exporter_id: str,
        code: str,
    ) -> None:
        """Submit local diagnostic I/O without touching the business loop."""

        if self._diagnostics is not None:
            self._diagnostics.offer(callback, envelope, exporter_id, code)

    def close_sync(self, *, drain: bool = False) -> None:
        # Stop accepting first. Drain must let already accepted work reach its
        # bounded terminal outcome before worker cancellation.
        self._accepting = False
        if drain:
            self._queue.join()
        self._stopped = True
        for worker in self._workers:
            worker.join(timeout=1)
        self._workers.clear()
        if self._diagnostics is not None:
            self._diagnostics.close()

        async def close_exporters() -> None:
            for exporter in self._exporters:
                closer = getattr(exporter, "aclose", None)
                if closer is not None:
                    with suppress(Exception):
                        await closer()

        asyncio.run(close_exporters())

    async def close(self, *, drain: bool = False) -> None:
        await asyncio.to_thread(self.close_sync, drain=drain)

    def _worker(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self._stopped:
                try:
                    exporter, envelope = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    task = loop.create_task(self._deliver(exporter, envelope))
                    while not task.done() and not self._stopped:
                        loop.run_until_complete(asyncio.sleep(0.01))
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        loop.run_until_complete(task)
                finally:
                    with self._pending_lock:
                        self._pending -= 1
                    self._queue.task_done()
        finally:
            loop.close()

    async def _deliver(
        self, exporter: ObservationExporter, envelope: ObservationEnvelope
    ) -> None:
        try:
            await asyncio.wait_for(
                exporter.export(envelope),
                timeout=self._policy.export_timeout_ms / 1_000,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._failure_callback is not None and self._diagnostics is not None:
                self._diagnostics.offer(
                    self._failure_callback,
                    envelope,
                    exporter.exporter_id,
                    type(exc).__name__,
                )


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
        if result is AppendOnceResult.APPENDED:
            self.offer_persisted(envelope)
        return result

    def offer_persisted(self, envelope: ObservationEnvelope) -> None:
        """Offer an already-authoritative local event exactly once."""

        self._redactor.assert_safe_model(envelope)
        if not envelope.export or self._dispatcher is None:
            return
        offered = self._dispatcher.offer(envelope)
        if offered is not ExporterOfferResult.ACCEPTED:
            # The original local event is already authoritative. A bounded
            # best-effort local diagnostic never enters the remote dispatcher.
            self._dispatcher.offer_diagnostic(
                self._record_delivery_dropped,
                envelope,
                "exporter_dispatcher",
                offered.value,
            )

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
