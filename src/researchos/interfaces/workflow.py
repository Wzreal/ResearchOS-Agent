"""Narrow durable workflow bridge ports."""

from typing import Protocol

from researchos.domain.workflow import WorkflowRuntimeHandoff


class WorkflowRuntimeHandoffStore(Protocol):
    def create(self, handoff: WorkflowRuntimeHandoff) -> None: ...

    def load(self, run_id: str) -> WorkflowRuntimeHandoff: ...

    def save(
        self, handoff: WorkflowRuntimeHandoff, *, expected_revision: int
    ) -> None: ...
