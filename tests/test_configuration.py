from datetime import timedelta

import pytest

from researchos.application.errors import RunConfigurationError
from researchos.domain.contracts import OperatingMode, RunConfig


def test_mock_mode_needs_no_provider_configuration(lifecycle: tuple) -> None:
    manager, _, _, _, run_input, _ = lifecycle
    assert manager.create(run_input, RunConfig()).config.mode is OperatingMode.MOCK


def test_real_mode_fails_before_any_persistence(lifecycle: tuple) -> None:
    manager, store, trace, clock, run_input, _ = lifecycle
    real = RunConfig(
        mode=OperatingMode.REAL,
        deadline=clock.now() + timedelta(hours=1),
    )

    with pytest.raises(RunConfigurationError, match="no configured real adapters"):
        manager.create(run_input, real)

    assert store._states == {}
    assert trace._events == {}
