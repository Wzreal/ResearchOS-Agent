"""Phase 4 Agent decision port."""

from typing import Protocol

from researchos.domain.agent import AgentDecision, AgentDescriptor, AgentRequest
from researchos.interfaces.runtime import CancellationSignal


class Agent(Protocol):
    @property
    def descriptor(self) -> AgentDescriptor: ...

    async def decide(
        self, request: AgentRequest, cancellation: CancellationSignal
    ) -> AgentDecision: ...
