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
    """One owned exporter loop with synchronous bounded ingress.

    A single background thread owns all async exporters and their clients.
    Callers only mutate a standard-library thread-safe queue; they never touch
    asyncio primitives or perform remote I/O.
    """

    _STOP = object()

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
        self._queue: queue.Queue[
            tuple[ObservationExporter, ObservationEnvelope] | object
        ] = queue.Queue()
        self._failure_callback = failure_callback
        self._diagnostics = (
            _DiagnosticDispatcher(
                max_pending=self._policy.max_pending_diagnostics,
                max_concurrency=self._policy.max_diagnostic_concurrency,
            )
            if self._exporters
            else None
        )
        self._state_lock = Lock()
        self._pending = 0
        self._drained = Event()
        self._drained.set()
        self._thread: Thread | None = None
        self._started = False
        self._accepting = True
        self._drain_on_stop = False
        self._closed = False
        self._owned_loop_id: int | None = None

    def start(self) -> None:
        with self._state_lock:
            if self._started or self._closed or not self._exporters:
                return
            self._started = True
            self._thread = Thread(
                target=self._thread_main,
                name="researchos-exporter-loop",
                daemon=True,
            )
            self._thread.start()

    def set_failure_callback(self, callback: FailureCallback) -> None:
        self._failure_callback = callback

    def offer(self, envelope: ObservationEnvelope) -> ExporterOfferResult:
        if not envelope.export or not self._exporters:
            return ExporterOfferResult.ACCEPTED
        self.start()
        required = len(self._exporters)
        with self._state_lock:
            if not self._accepting or self._closed:
                return ExporterOfferResult.DISPATCHER_STOPPED
            if self._pending + required > self._policy.max_pending_deliveries:
                return ExporterOfferResult.DROPPED_QUEUE_FULL
            self._pending += required
            self._drained.clear()
            for exporter in self._exporters:
                self._queue.put_nowait((exporter, envelope))
        return ExporterOfferResult.ACCEPTED

    @property
    def worker_count(self) -> int:
        return int(self._thread is not None)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def owned_loop_id(self) -> int | None:
        return self._owned_loop_id

    def offer_diagnostic(
        self,
        callback: FailureCallback,
        envelope: ObservationEnvelope,
        exporter_id: str,
        code: str,
    ) -> None:
        if self._diagnostics is not None:
            self._diagnostics.offer(callback, envelope, exporter_id, code)

    def close_sync(self, *, drain: bool = False) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._accepting = False
            self._drain_on_stop = drain
            if not self._started and self._exporters:
                # Ensure exporter cleanup happens in its one owned loop even
                # when no observation was ever offered.
                self._started = True
                self._thread = Thread(
                    target=self._thread_main,
                    name="researchos-exporter-loop",
                    daemon=True,
                )
                self._thread.start()
            self._queue.put_nowait(self._STOP)
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._state_lock:
            self._closed = True
        if self._diagnostics is not None:
            self._diagnostics.close()

    async def close(self, *, drain: bool = False) -> None:
        await asyncio.to_thread(self.close_sync, drain=drain)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._owned_loop_id = id(loop)
        try:
            loop.run_until_complete(self._run())
            loop.run_until_complete(self._close_exporters())
        finally:
            loop.close()

    async def _run(self) -> None:
        semaphore = asyncio.Semaphore(self._policy.max_concurrency)
        tasks: set[asyncio.Task[None]] = set()
        stopping = False
        while not stopping:
            item = await asyncio.to_thread(self._queue.get)
            if item is self._STOP:
                stopping = True
                continue
            exporter, envelope = item
            task = asyncio.create_task(self._deliver(exporter, envelope, semaphore))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if self._drain_on_stop:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        else:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            while True:
                try:
                    discarded = self._queue.get_nowait()
                except queue.Empty:
                    break
                if discarded is not self._STOP:
                    self._settle_one()
        while not self._drained.is_set():
            await asyncio.sleep(0)

    async def _deliver(
        self,
        exporter: ObservationExporter,
        envelope: ObservationEnvelope,
        semaphore: asyncio.Semaphore,
    ) -> None:
        try:
            async with semaphore:
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
        finally:
            self._settle_one()

    def _settle_one(self) -> None:
        with self._state_lock:
            self._pending -= 1
            if self._pending == 0:
                self._drained.set()

    async def _close_exporters(self) -> None:
        for exporter in self._exporters:
            closer = getattr(exporter, "aclose", None)
            if closer is not None:
                with suppress(Exception):
                    await closer()


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
