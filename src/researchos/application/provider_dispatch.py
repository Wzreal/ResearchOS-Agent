"""Read-only lifecycle authorization immediately before REAL dispatch."""

from researchos.application.errors import RunConfigurationError
from researchos.application.real_composition import RealCompositionManager
from researchos.domain.contracts import OperatingMode, RunStatus
from researchos.interfaces.lifecycle import RunStore


class RunLifecycleDispatchAuthorizer:
    def __init__(self, runs: RunStore, compositions: RealCompositionManager) -> None:
        self._runs = runs
        self._compositions = compositions

    def authorize(
        self,
        *,
        run_id: str,
        role_id: str,
        composition_hash: str | None = None,
        trusted_runtime_replan: bool = False,
    ) -> None:
        state = self._runs.load(run_id)
        if state.config.mode is not OperatingMode.REAL:
            raise RunConfigurationError("REAL provider requires a REAL Run")
        allowed = {
            "planning": (
                {RunStatus.RUNNING}
                if trusted_runtime_replan
                else {RunStatus.PLANNING}
            ),
            "agent": {RunStatus.RUNNING},
            "verification": {RunStatus.VERIFYING},
        }
        if role_id not in allowed:
            raise RunConfigurationError("unsupported REAL provider role")
        if state.status not in allowed[role_id]:
            raise RunConfigurationError(
                f"REAL {role_id} dispatch is not allowed from {state.status.value}"
            )
        if composition_hash is not None:
            self._compositions.validate_dispatch(state, composition_hash)
