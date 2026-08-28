"""Exact deterministic Tool fixtures with no default success."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from researchos.application.errors import AgentContractError
from researchos.domain.tools import (
    AdapterMode,
    ToolDescriptor,
    ToolInvocationRequest,
    ToolInvocationResult,
)
from researchos.interfaces.runtime import CancellationSignal


class ToolFixtureAction(StrEnum):
    RETURN = "return"
    WAIT_FOR_CANCELLATION = "wait_for_cancellation"
    NEVER_RETURN = "never_return"
    RAISE = "raise"


@dataclass(frozen=True, slots=True)
class ToolFixtureKey:
    tool_operation_key: str


@dataclass(frozen=True, slots=True)
class ToolFixture:
    result: ToolInvocationResult | None = None
    action: ToolFixtureAction = ToolFixtureAction.RETURN


class MockTool:
    def __init__(
        self,
        descriptor: ToolDescriptor,
        fixtures: Mapping[ToolFixtureKey, ToolFixture],
    ) -> None:
        if descriptor.mode is not AdapterMode.MOCK:
            raise AgentContractError("mock Tool requires mock adapter mode")
        self._descriptor = descriptor
        self._fixtures = dict(fixtures)
        self.requests: list[ToolInvocationRequest] = []

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    async def invoke(
        self, request: ToolInvocationRequest, cancellation: CancellationSignal
    ) -> ToolInvocationResult:
        self.requests.append(request)
        try:
            fixture = self._fixtures[ToolFixtureKey(request.tool_operation_key)]
        except KeyError as exc:
            raise AgentContractError("no exact mock Tool fixture") from exc
        if fixture.action is ToolFixtureAction.WAIT_FOR_CANCELLATION:
            await cancellation.wait()
            raise asyncio.CancelledError
        if fixture.action is ToolFixtureAction.NEVER_RETURN:
            await asyncio.Future()
        if fixture.action is ToolFixtureAction.RAISE:
            raise AgentContractError("mock Tool failure")
        if fixture.result is None:
            raise AgentContractError("return fixture requires a result")
        return fixture.result.model_copy(deep=True)
