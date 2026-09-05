"""In-memory CAS store for the Phase 10 planning/runtime bridge."""

from threading import RLock

from researchos.application.errors import (
    CorruptWorkflowHandoff,
    WorkflowHandoffAlreadyExists,
    WorkflowHandoffNotFound,
    WorkflowHandoffRevisionConflict,
)
from researchos.domain.workflow import WorkflowRuntimeHandoff
from researchos.security.redaction import PersistenceRedactor


class InMemoryWorkflowRuntimeHandoffStore:
    def __init__(self) -> None:
        self._items: dict[str, WorkflowRuntimeHandoff] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, handoff: WorkflowRuntimeHandoff) -> None:
        self._validate(handoff)
        with self._lock:
            if handoff.run_id in self._items:
                raise WorkflowHandoffAlreadyExists(handoff.run_id)
            if handoff.handoff_revision != 0:
                raise WorkflowHandoffRevisionConflict(
                    "initial handoff revision must be zero"
                )
            self._items[handoff.run_id] = handoff.model_copy(deep=True)

    def load(self, run_id: str) -> WorkflowRuntimeHandoff:
        with self._lock:
            try:
                handoff = self._items[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise WorkflowHandoffNotFound(run_id) from exc
        self._validate(handoff)
        if handoff.run_id != run_id:
            raise CorruptWorkflowHandoff("handoff run identity differs")
        return handoff

    def save(self, handoff: WorkflowRuntimeHandoff, *, expected_revision: int) -> None:
        self._validate(handoff)
        with self._lock:
            try:
                current = self._items[handoff.run_id]
            except KeyError as exc:
                raise WorkflowHandoffNotFound(handoff.run_id) from exc
            if current.handoff_id != handoff.handoff_id:
                raise WorkflowHandoffRevisionConflict("handoff identity changed")
            if current.handoff_revision != expected_revision:
                raise WorkflowHandoffRevisionConflict("handoff revision differs")
            if handoff.handoff_revision != expected_revision + 1:
                raise WorkflowHandoffRevisionConflict(
                    "handoff revision must increment once"
                )
            self._items[handoff.run_id] = handoff.model_copy(deep=True)

    def _validate(self, handoff: WorkflowRuntimeHandoff) -> None:
        self._redactor.assert_safe_model(handoff)
        try:
            validated = WorkflowRuntimeHandoff.model_validate(
                handoff.model_dump(mode="python")
            )
        except ValueError as exc:
            raise CorruptWorkflowHandoff("workflow handoff is invalid") from exc
        if validated != handoff:
            raise CorruptWorkflowHandoff("workflow handoff canonical form differs")
