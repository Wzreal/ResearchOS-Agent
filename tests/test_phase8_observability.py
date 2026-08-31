from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from conftest import FrozenClock
from test_phase5_evidence_claims import NOW

from researchos.adapters import filesystem as filesystem_adapter
from researchos.adapters.filesystem import FilesystemTraceSink
from researchos.adapters.memory import InMemoryTraceSink
from researchos.adapters.mock_observability import MockObservationExporter
from researchos.application.errors import ObservationCorruption
from researchos.application.observation_recorder import (
    ExporterDispatcher,
    ObservationRecorder,
)
from researchos.domain.contracts import TraceEvent, TraceEventType
from researchos.domain.identity import stable_id
from researchos.domain.observability import AppendOnceResult, ExporterPolicy
from researchos.domain.runtime import RuntimeResourceAmount, TraceEventDescriptor


def _descriptor(event_id: str, *, value: str = "safe") -> TraceEventDescriptor:
    return TraceEventDescriptor.from_event(
        TraceEvent(
            event_id=event_id,
            event_type=TraceEventType.VERIFICATION_STARTED,
            timestamp=NOW,
            run_id="run_phase8",
            revision=4,
            correlation_id="ver_phase8",
            attributes={"value": value},
        )
    )


@pytest.mark.parametrize("kind", ["memory", "filesystem"])
def test_append_once_equal_is_noop_and_different_hash_is_corruption(
    kind, tmp_path
) -> None:
    sink = InMemoryTraceSink() if kind == "memory" else FilesystemTraceSink(tmp_path)
    original = _descriptor("evt_phase8")
    assert sink.append_once(original) is AppendOnceResult.APPENDED
    assert sink.append_once(original) is AppendOnceResult.ALREADY_PRESENT
    assert len(sink.read("run_phase8")) == 1
    with pytest.raises(ObservationCorruption):
        sink.append_once(_descriptor("evt_phase8", value="changed"))


def test_filesystem_append_once_is_shared_across_sink_instances(tmp_path) -> None:
    def delay_before_append(stage: str) -> None:
        if stage == "before_trace_append":
            time.sleep(0.05)

    first = FilesystemTraceSink(tmp_path, fault_injector=delay_before_append)
    second = FilesystemTraceSink(tmp_path, fault_injector=delay_before_append)
    descriptor = _descriptor("evt_concurrent_append_once")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda sink: sink.append_once(descriptor), (first, second))
        )
    assert sorted(results) == sorted(
        (AppendOnceResult.APPENDED, AppendOnceResult.ALREADY_PRESENT)
    )
    assert len(first.read("run_phase8")) == 1
    assert filesystem_adapter._TRACE_PATH_LOCKS == {}


def test_trace_append_crash_before_delivered_flag_replays_without_duplicate() -> None:
    from test_phase6_synthesis_verification import prepared

    from researchos.adapters.verification_operation_memory import (
        InMemoryVerificationOperationStore,
    )
    from researchos.application.verification_input import compute_verification_id
    from researchos.application.verification_operation import (
        VerificationOperationManager,
    )

    class FailDeliveredOnceStore(InMemoryVerificationOperationStore):
        fail = True

        def save(self, operation, *, expected_revision):
            current = self.load(operation.run_id)
            delivered_only = (
                current.outbox != operation.outbox
                and len(current.outbox) == len(operation.outbox)
                and any(item.delivered for item in operation.outbox)
            )
            if self.fail and delivered_only:
                self.fail = False
                raise RuntimeError("crash before delivered checkpoint")
            return super().save(operation, expected_revision=expected_revision)

    _, policy, frozen, _, _, _ = prepared()
    store = FailDeliveredOnceStore()
    trace = InMemoryTraceSink()
    recorder = ObservationRecorder(trace_sink=trace, clock=FrozenClock(NOW))
    manager = VerificationOperationManager(
        store=store, clock=FrozenClock(NOW), recorder=recorder
    )
    model_hash = stable_id("bundle", ["phase8"])[-64:]
    verification_id = compute_verification_id(frozen, policy, model_hash)
    operation = manager.create(
        frozen=frozen,
        policy=policy,
        hard_limits=RuntimeResourceAmount(tokens=1_000),
        model_bundle_hash=model_hash,
        verification_id=verification_id,
    )
    assert operation.outbox[0].delivered is False
    assert len(trace.read(frozen.run_id)) == 1
    manager.reconcile_outbox(frozen.run_id)
    assert len(trace.read(frozen.run_id)) == 1
    assert store.load(frozen.run_id).outbox[0].delivered is True


def test_slow_exporter_is_not_on_business_critical_path() -> None:
    async def scenario() -> None:
        exporter = MockObservationExporter(delay_seconds=5)
        dispatcher = ExporterDispatcher(
            exporters=(exporter,),
            policy=ExporterPolicy(export_timeout_ms=10_000),
        )
        recorder = ObservationRecorder(
            trace_sink=InMemoryTraceSink(),
            clock=FrozenClock(NOW),
            dispatcher=dispatcher,
        )
        started = time.perf_counter()
        recorder.record_descriptor(_descriptor("evt_slow"))
        elapsed = time.perf_counter() - started
        assert elapsed < 0.1
        await dispatcher.close(drain=False)

    asyncio.run(scenario())


def test_queue_full_drops_remote_only_and_records_local_diagnostic() -> None:
    async def scenario() -> None:
        trace = InMemoryTraceSink()
        exporter = MockObservationExporter(delay_seconds=5)
        dispatcher = ExporterDispatcher(
            exporters=(exporter,),
            policy=ExporterPolicy(
                max_pending_deliveries=1,
                max_concurrency=1,
                export_timeout_ms=10_000,
            ),
        )
        recorder = ObservationRecorder(
            trace_sink=trace, clock=FrozenClock(NOW), dispatcher=dispatcher
        )
        recorder.record_descriptor(_descriptor("evt_queue_a"))
        recorder.record_descriptor(_descriptor("evt_queue_b"))
        for _ in range(100):
            if any(
                item.event_type is TraceEventType.OBSERVABILITY_DELIVERY_DROPPED
                for item in trace.read("run_phase8")
            ):
                break
            await asyncio.sleep(0.001)
        types = [item.event_type for item in trace.read("run_phase8")]
        assert TraceEventType.OBSERVABILITY_DELIVERY_DROPPED in types
        assert (
            len(
                [
                    item
                    for item in types
                    if item is TraceEventType.VERIFICATION_STARTED
                ]
            )
            == 2
        )
        await dispatcher.close(drain=False)

    asyncio.run(scenario())


def test_export_failure_diagnostic_is_local_only_and_non_recursive() -> None:
    async def scenario() -> None:
        trace = InMemoryTraceSink()
        exporter = MockObservationExporter(fail=True)
        dispatcher = ExporterDispatcher(exporters=(exporter,))
        recorder = ObservationRecorder(
            trace_sink=trace, clock=FrozenClock(NOW), dispatcher=dispatcher
        )
        recorder.record_descriptor(_descriptor("evt_failure"))
        await asyncio.sleep(0.02)
        events = trace.read("run_phase8")
        failures = [
            item
            for item in events
            if item.event_type is TraceEventType.OBSERVABILITY_EXPORT_FAILED
        ]
        assert len(failures) == 1
        assert failures[0].attributes["export"] is False
        assert exporter.calls == 1
        await dispatcher.close(drain=False)

    asyncio.run(scenario())


def test_export_backpressure_diagnostic_failure_cannot_fail_business() -> None:
    class DiagnosticFailTrace(InMemoryTraceSink):
        def append_once(self, descriptor):
            if descriptor.event_type is TraceEventType.OBSERVABILITY_DELIVERY_DROPPED:
                raise OSError("diagnostic disk failure")
            return super().append_once(descriptor)

    async def scenario() -> None:
        trace = DiagnosticFailTrace()
        dispatcher = ExporterDispatcher(
            exporters=(MockObservationExporter(delay_seconds=5),),
            policy=ExporterPolicy(max_pending_deliveries=1, max_concurrency=1),
        )
        recorder = ObservationRecorder(
            trace_sink=trace, clock=FrozenClock(NOW), dispatcher=dispatcher
        )
        recorder.record_descriptor(_descriptor("evt_diag_a"))
        recorder.record_descriptor(_descriptor("evt_diag_b"))
        assert len(trace.read("run_phase8")) == 2
        await dispatcher.close(drain=False)

    asyncio.run(scenario())


def test_export_offer_without_event_loop_is_isolated() -> None:
    trace = InMemoryTraceSink()
    dispatcher = ExporterDispatcher(exporters=(MockObservationExporter(),))
    recorder = ObservationRecorder(
        trace_sink=trace, clock=FrozenClock(NOW), dispatcher=dispatcher
    )
    recorder.record_descriptor(_descriptor("evt_sync_offer"))
    for _ in range(100):
        events = trace.read("run_phase8")
        if len(events) > 1:
            break
        time.sleep(0.001)
    assert events[0].event_id == "evt_sync_offer"
    assert events[-1].event_type is TraceEventType.OBSERVABILITY_DELIVERY_DROPPED


def test_queue_full_slow_filesystem_diagnostic_never_blocks_event_loop(
    tmp_path,
) -> None:
    diagnostic_started = Event()
    release_diagnostic = Event()

    class BlockingDropTrace(FilesystemTraceSink):
        def append_once(self, descriptor):
            if descriptor.event_type is TraceEventType.OBSERVABILITY_DELIVERY_DROPPED:
                diagnostic_started.set()
                release_diagnostic.wait(timeout=5)
            return super().append_once(descriptor)

    async def scenario() -> float:
        dispatcher = ExporterDispatcher(
            exporters=(MockObservationExporter(delay_seconds=5),),
            policy=ExporterPolicy(
                max_pending_deliveries=1,
                max_concurrency=1,
                export_timeout_ms=10_000,
            ),
        )
        recorder = ObservationRecorder(
            trace_sink=BlockingDropTrace(tmp_path),
            clock=FrozenClock(NOW),
            dispatcher=dispatcher,
        )
        recorder.record_descriptor(_descriptor("evt_full_a"))
        started = time.perf_counter()
        recorder.record_descriptor(_descriptor("evt_full_b"))
        elapsed = time.perf_counter() - started
        while not diagnostic_started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        release_diagnostic.set()
        await dispatcher.close(drain=False)
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.1


def test_export_failure_slow_filesystem_diagnostic_never_blocks_event_loop(
    tmp_path,
) -> None:
    diagnostic_started = Event()
    release_diagnostic = Event()

    class BlockingDiagnosticTrace(FilesystemTraceSink):
        def append_once(self, descriptor):
            if descriptor.event_type is TraceEventType.OBSERVABILITY_EXPORT_FAILED:
                diagnostic_started.set()
                release_diagnostic.wait(timeout=5)
            return super().append_once(descriptor)

    async def scenario() -> float:
        exporter = MockObservationExporter(fail=True)
        dispatcher = ExporterDispatcher(exporters=(exporter,))
        recorder = ObservationRecorder(
            trace_sink=BlockingDiagnosticTrace(tmp_path),
            clock=FrozenClock(NOW),
            dispatcher=dispatcher,
        )
        started = time.perf_counter()
        recorder.record_descriptor(_descriptor("evt_slow_diagnostic"))
        while not diagnostic_started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - started
        release_diagnostic.set()
        await dispatcher.close(drain=False)
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.5


def test_identical_exporter_failures_have_distinct_diagnostic_identity() -> None:
    async def scenario() -> tuple[TraceEvent, ...]:
        trace = InMemoryTraceSink()
        dispatcher = ExporterDispatcher(
            exporters=(
                MockObservationExporter(exporter_id="exporter_a", fail=True),
                MockObservationExporter(exporter_id="exporter_b", fail=True),
            )
        )
        recorder = ObservationRecorder(
            trace_sink=trace, clock=FrozenClock(NOW), dispatcher=dispatcher
        )
        recorder.record_descriptor(_descriptor("evt_two_exporters"))
        for _ in range(100):
            failures = tuple(
                item
                for item in trace.read("run_phase8")
                if item.event_type is TraceEventType.OBSERVABILITY_EXPORT_FAILED
            )
            if len(failures) == 2:
                await dispatcher.close(drain=False)
                return failures
            await asyncio.sleep(0.01)
        await dispatcher.close(drain=False)
        return ()

    failures = asyncio.run(scenario())
    assert len(failures) == 2
    assert len({item.event_id for item in failures}) == 2
    assert {item.attributes["exporter_id"] for item in failures} == {
        "exporter_a",
        "exporter_b",
    }
