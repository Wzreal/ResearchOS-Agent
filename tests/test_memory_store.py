import pytest

from researchos.application.errors import (
    RevisionConflict,
    RunAlreadyExists,
    RunNotFound,
)
from researchos.domain.contracts import RunStatus


def test_memory_store_contract_and_deep_copy(lifecycle: tuple) -> None:
    manager, store, _, _, run_input, config = lifecycle
    state = manager.create(run_input, config)
    loaded = store.load(state.run_id)

    assert loaded is not state
    assert loaded == state
    with pytest.raises(RunAlreadyExists):
        store.create(state)
    with pytest.raises(RunNotFound):
        store.load("run_missing")


def test_memory_store_compare_and_set(lifecycle: tuple) -> None:
    manager, store, _, clock, run_input, config = lifecycle
    state = manager.create(run_input, config)
    raw = state.model_dump(mode="python")
    raw.update(
        revision=1,
        status=RunStatus.PLANNING,
        updated_at=clock.now(),
        last_transition_id="tr_manual",
    )
    changed = type(state).model_validate(raw)

    with pytest.raises(RevisionConflict):
        store.save(changed, expected_revision=9)
