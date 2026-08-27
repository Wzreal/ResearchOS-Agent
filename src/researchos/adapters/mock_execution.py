"""Exact-script offline task backend; missing fixtures fail explicitly."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from researchos.application.errors import RuntimePreconditionError
from researchos.domain.runtime import (
    ExecutionResultStatus,
    RuntimeResourceAmount,
    TaskExecutionRequest,
    TaskExecutionResult,
    UsageCertainty,
)
from researchos.interfaces.runtime import CancellationSignal


class MockAction(StrEnum):
    RETURN = "return"
    WAIT_FOR_CANCELLATION = "wait_for_cancellation"
    NEVER_RETURN = "never_return"


@dataclass(frozen=True, slots=True)
class ExecutionFixtureKey:
    task_id: str
    attempt_number: int


@dataclass(frozen=True, slots=True)
class ExecutionFixture:
    result: TaskExecutionResult | None = None
    action: MockAction = MockAction.RETURN
    delay_milliseconds: int = 0


class MockTaskExecutionBackend:
    def __init__(
        self, fixtures: Mapping[ExecutionFixtureKey, ExecutionFixture]
    ) -> None:
        self._fixtures = dict(fixtures)
        self.requests: list[TaskExecutionRequest] = []
        self.active = 0
        self.max_active = 0
        self.cancelled_attempts: list[str] = []
        self._seen_attempt_keys: set[str] = set()

    async def execute(
        self, request: TaskExecutionRequest, cancellation: CancellationSignal
    ) -> TaskExecutionResult:
        key = ExecutionFixtureKey(request.task_id, request.attempt_number)
        try:
            fixture = self._fixtures[key]
        except KeyError as exc:
            raise RuntimePreconditionError(
                f"no exact mock execution fixture for {key}"
            ) from exc
        if request.attempt_key in self._seen_attempt_keys:
            raise RuntimePreconditionError("duplicate attempt key dispatched")
        self._seen_attempt_keys.add(request.attempt_key)
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if fixture.action is MockAction.WAIT_FOR_CANCELLATION:
                await cancellation.wait()
                self.cancelled_attempts.append(request.attempt_id)
                return TaskExecutionResult(
                    status=ExecutionResultStatus.FAILED,
                    failure_code="cancelled",
                    usage_certainty=UsageCertainty.UNKNOWN,
                )
            if fixture.action is MockAction.NEVER_RETURN:
                await asyncio.Future()
            if fixture.delay_milliseconds:
                await asyncio.sleep(fixture.delay_milliseconds / 1_000)
            if fixture.result is None:
                raise RuntimePreconditionError("return fixture requires a result")
            return fixture.result.model_copy(deep=True)
        finally:
            self.active -= 1


def successful_result(
    *,
    usage: RuntimeResourceAmount | None = None,
    output_ids: tuple[str, ...] = (),
    receipt: str | None = None,
) -> TaskExecutionResult:
    return TaskExecutionResult(
        status=ExecutionResultStatus.SUCCEEDED,
        usage=usage or RuntimeResourceAmount(),
        usage_certainty=UsageCertainty.EXACT,
        produced_output_ids=output_ids,
        backend_receipt=receipt,
    )
