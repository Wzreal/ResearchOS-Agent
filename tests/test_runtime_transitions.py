import pytest

from researchos.application.errors import RuntimePreconditionError
from researchos.application.runtime_transitions import (
    LEGAL_ATTEMPT_TRANSITIONS,
    LEGAL_TASK_TRANSITIONS,
    validate_attempt_transition,
    validate_task_transition,
)
from researchos.domain.runtime import AttemptStatus, TaskStatus


@pytest.mark.parametrize(
    ("previous", "target"),
    [
        (previous, target)
        for previous in TaskStatus
        for target in TaskStatus
        if target not in LEGAL_TASK_TRANSITIONS[previous]
    ],
)
def test_all_illegal_task_transitions_are_rejected(
    previous: TaskStatus, target: TaskStatus
) -> None:
    with pytest.raises(RuntimePreconditionError, match="illegal task transition"):
        validate_task_transition(previous, target)


@pytest.mark.parametrize(
    ("previous", "target"),
    [
        (previous, target)
        for previous in AttemptStatus
        for target in AttemptStatus
        if target not in LEGAL_ATTEMPT_TRANSITIONS[previous]
    ],
)
def test_all_illegal_attempt_transitions_are_rejected(
    previous: AttemptStatus, target: AttemptStatus
) -> None:
    with pytest.raises(RuntimePreconditionError, match="illegal attempt transition"):
        validate_attempt_transition(previous, target)


def test_all_declared_runtime_transitions_are_accepted() -> None:
    for previous, targets in LEGAL_TASK_TRANSITIONS.items():
        for target in targets:
            validate_task_transition(previous, target)
    for previous, targets in LEGAL_ATTEMPT_TRANSITIONS.items():
        for target in targets:
            validate_attempt_transition(previous, target)
