"""Exact scripted Agent with controllable async failure behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter, ValidationError

from researchos.application.errors import AgentContractError
from researchos.domain.agent import AgentDecision, AgentDescriptor, AgentRequest
from researchos.domain.tools import AdapterMode
from researchos.interfaces.runtime import CancellationSignal


class AgentFixtureAction(StrEnum):
    RETURN = "return"
    WAIT_FOR_CANCELLATION = "wait_for_cancellation"
    NEVER_RETURN = "never_return"
    RAISE = "raise"


@dataclass(frozen=True, slots=True)
class AgentFixtureKey:
    task_id: str
    attempt_number: int
    agent_step: int


@dataclass(frozen=True, slots=True)
class AgentFixture:
    decision: AgentDecision | dict[str, Any] | None = None
    action: AgentFixtureAction = AgentFixtureAction.RETURN


class ScriptedAgent:
    def __init__(
        self,
        descriptor: AgentDescriptor,
        fixtures: Mapping[AgentFixtureKey, AgentFixture],
    ) -> None:
        if descriptor.mode is not AdapterMode.MOCK:
            raise AgentContractError("scripted Agent requires mock adapter mode")
        self._descriptor = descriptor
        self._fixtures = dict(fixtures)
        self.requests: list[AgentRequest] = []

    @property
    def descriptor(self) -> AgentDescriptor:
        return self._descriptor

    async def decide(
        self, request: AgentRequest, cancellation: CancellationSignal
    ) -> AgentDecision:
        key = AgentFixtureKey(
            request.context.task_id,
            request.context.attempt_number,
            request.agent_step,
        )
        self.requests.append(request)
        try:
            fixture = self._fixtures[key]
        except KeyError as exc:
            raise AgentContractError("no exact scripted Agent fixture") from exc
        if fixture.action is AgentFixtureAction.WAIT_FOR_CANCELLATION:
            await cancellation.wait()
            raise AgentContractError("scripted Agent cancelled")
        if fixture.action is AgentFixtureAction.NEVER_RETURN:
            await asyncio.Future()
        if fixture.action is AgentFixtureAction.RAISE:
            raise AgentContractError("scripted Agent failure")
        if fixture.decision is None:
            raise AgentContractError("return fixture requires a decision")
        try:
            return TypeAdapter(AgentDecision).validate_python(fixture.decision)
        except ValidationError as exc:
            raise AgentContractError("scripted Agent decision is malformed") from exc
