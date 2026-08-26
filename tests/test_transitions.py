import pytest

from researchos.application.errors import InvalidTransition
from researchos.application.run_manager import LEGAL_TRANSITIONS
from researchos.domain.contracts import RunStatus

EXPECTED = {
    RunStatus.CREATED: {RunStatus.PLANNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.PLANNING: {RunStatus.READY, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.READY: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.VERIFYING,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.VERIFYING: {
        RunStatus.EVALUATING,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.EVALUATING: {
        RunStatus.COMPLETED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.COMPLETED: set(),
    RunStatus.PARTIAL: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


@pytest.mark.parametrize("current", list(RunStatus))
@pytest.mark.parametrize("target", list(RunStatus))
def test_complete_transition_matrix(current: RunStatus, target: RunStatus) -> None:
    assert (target in LEGAL_TRANSITIONS[current]) is (target in EXPECTED[current])


def test_transition_api_rejects_terminal_target(lifecycle: tuple) -> None:
    manager, _, _, _, run_input, config = lifecycle
    state = manager.create(run_input, config)

    with pytest.raises(InvalidTransition, match="finalize"):
        manager.transition(state.run_id, RunStatus.FAILED)
