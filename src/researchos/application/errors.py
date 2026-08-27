"""Explicit Phase 1 application and persistence failures."""


class ResearchOSError(Exception):
    """Base class for expected ResearchOS failures."""


class RunNotFound(ResearchOSError):
    pass


class RunAlreadyExists(ResearchOSError):
    pass


class InvalidTransition(ResearchOSError):
    pass


class RevisionConflict(ResearchOSError):
    pass


class CorruptRunState(ResearchOSError):
    pass


class IncompatibleSchema(ResearchOSError):
    pass


class RunConfigurationError(ResearchOSError):
    pass


class TerminalRunCannotResume(ResearchOSError):
    pass


class UnsafePersistenceData(ResearchOSError):
    pass


class StatePersistenceError(ResearchOSError):
    def __init__(self, message: str, *, run_id: str, state_replaced: bool) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.state_replaced = state_replaced


class TraceCommitError(ResearchOSError):
    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        transition_id: str,
        revision: int,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.transition_id = transition_id
        self.revision = revision
        self.state_committed = True


class PlanningPreconditionError(ResearchOSError):
    """Planning was requested outside its Phase 2 lifecycle boundary."""


class PlanningModelFailure(ResearchOSError):
    """Stable provider-independent model boundary failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CheckpointNotFound(ResearchOSError):
    pass


class CheckpointAlreadyExists(ResearchOSError):
    pass


class CheckpointRevisionConflict(ResearchOSError):
    pass


class CorruptCheckpoint(ResearchOSError):
    pass


class CheckpointCompatibilityError(ResearchOSError):
    pass


class CheckpointPersistenceError(ResearchOSError):
    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        checkpoint_revision: int,
        checkpoint_replaced: bool,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.checkpoint_revision = checkpoint_revision
        self.checkpoint_replaced = checkpoint_replaced


class RuntimePreconditionError(ResearchOSError):
    pass


class RuntimeTraceCommitError(ResearchOSError):
    def __init__(self, message: str, *, run_id: str, checkpoint_revision: int) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.checkpoint_revision = checkpoint_revision
        self.checkpoint_committed = True


class ReplanLineageConflict(ResearchOSError):
    pass
