"""Checkpoint-trusted bridge for restoring Phase 2 planner lineage."""

from researchos.application.checkpoint_manager import CheckpointManager
from researchos.application.errors import (
    CheckpointCompatibilityError,
    RuntimePreconditionError,
)
from researchos.domain.contracts import RunState, RunStatus, model_sha256
from researchos.domain.planning import ReplanRequest
from researchos.interfaces.planning import TrustedReplanLineageRestorer


class ReplanLineageRecovery:
    """Restore a pending lineage after all durable identities are verified.

    This service is deliberately separate from ``AsyncDAGExecutor``. It does
    not invoke planning, consume the pending request, or apply a replacement
    DAG.
    """

    def __init__(
        self,
        *,
        checkpoints: CheckpointManager,
        restorer: TrustedReplanLineageRestorer,
    ) -> None:
        self._checkpoints = checkpoints
        self._restorer = restorer

    def restore(self, state: RunState) -> ReplanRequest:
        if state.status is not RunStatus.RUNNING:
            raise RuntimePreconditionError(
                "replan lineage recovery requires RunStatus.RUNNING"
            )
        checkpoint = self._checkpoints.load_and_reconcile(state.run_id)
        if (
            checkpoint.run_id != state.run_id
            or checkpoint.run_revision != state.revision
            or checkpoint.dag.run_id != state.run_id
            or checkpoint.dag_hash != model_sha256(checkpoint.dag)
            or checkpoint.execution_policy_hash
            != model_sha256(checkpoint.execution_policy)
        ):
            raise CheckpointCompatibilityError(
                "checkpoint is incompatible with replan lineage recovery"
            )
        request = checkpoint.replan.pending_request
        if request is None or request.context != checkpoint.replan.context:
            raise RuntimePreconditionError("checkpoint has no valid pending replan")
        self._restorer.restore_trusted_lineage(checkpoint.replan.context)
        return request.model_copy(deep=True)
