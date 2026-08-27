from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from researchos.domain.contracts import TraceEvent, TraceEventType
from researchos.domain.runtime import (
    AttemptStatus,
    ExecutionPolicy,
    IdempotencyMode,
    RuntimeResourceAmount,
    TaskAttempt,
    TaskRuntimePolicy,
    TraceEventDescriptor,
    UsageCertainty,
)


def test_trace_descriptor_round_trips_exact_event_and_timestamp() -> None:
    event = TraceEvent(
        event_id="evt_exact",
        event_type=TraceEventType.TASK_READY,
        timestamp=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        run_id="run_test",
        revision=4,
        correlation_id="mutation_test",
        causation_id="evt_prior",
        attributes={"task_id": "task_a", "order": 1},
    )

    descriptor = TraceEventDescriptor.from_event(event)

    assert descriptor.to_event() == event
    assert descriptor.timestamp == event.timestamp


def test_trace_descriptor_rejects_modified_content() -> None:
    event = TraceEvent(
        event_id="evt_exact",
        event_type=TraceEventType.TASK_READY,
        timestamp=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        run_id="run_test",
        revision=4,
    )
    raw = TraceEventDescriptor.from_event(event).model_dump(mode="python")
    raw["attributes"] = {"changed": True}

    with pytest.raises(ValidationError, match="descriptor hash"):
        TraceEventDescriptor.model_validate(raw)


def test_operation_version_and_retry_contracts_are_explicit() -> None:
    with pytest.raises(ValidationError, match="max_attempts - 1"):
        TaskRuntimePolicy(
            task_id="task_a",
            operation_version="v1",
            max_attempts=2,
            timeout_milliseconds=100,
            reservation=RuntimeResourceAmount(duration_milliseconds=100),
        )
    with pytest.raises(ValidationError, match="non-idempotent"):
        TaskRuntimePolicy(
            task_id="task_a",
            operation_version="v1",
            max_attempts=2,
            backoff_milliseconds=(0,),
            timeout_milliseconds=100,
            reservation=RuntimeResourceAmount(duration_milliseconds=100),
            idempotency=IdempotencyMode.NON_IDEMPOTENT,
        )


def test_execution_policy_rejects_duplicate_task_contracts() -> None:
    policy = TaskRuntimePolicy(
        task_id="task_a",
        operation_version="v1",
        timeout_milliseconds=100,
        reservation=RuntimeResourceAmount(duration_milliseconds=100),
    )
    with pytest.raises(ValidationError, match="duplicate task"):
        ExecutionPolicy(max_concurrency=1, tasks=(policy, policy))


def test_unknown_attempt_usage_cannot_claim_an_amount() -> None:
    with pytest.raises(ValidationError, match="unknown usage"):
        TaskAttempt(
            attempt_id="attempt_a",
            task_id="task_a",
            attempt_number=1,
            operation_key="a" * 64,
            attempt_key="b" * 64,
            status=AttemptStatus.FAILED,
            started_at=datetime(2026, 8, 27, tzinfo=UTC),
            finished_at=datetime(2026, 8, 27, tzinfo=UTC),
            usage=RuntimeResourceAmount(tokens=1),
            usage_certainty=UsageCertainty.UNKNOWN,
        )
