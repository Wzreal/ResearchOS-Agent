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
    def __init__(
        self, message: str, *, run_id: str, state_replaced: bool
    ) -> None:
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
