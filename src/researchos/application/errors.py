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


class CapabilityConfigurationError(ResearchOSError):
    pass


class UnknownCapabilityError(ResearchOSError):
    pass


class ToolPermissionDenied(ResearchOSError):
    pass


class AgentContractError(ResearchOSError):
    pass


class AgentTraceError(ResearchOSError):
    pass


class ArtifactConflictError(ResearchOSError):
    pass


class MalformedCorpusError(ResearchOSError):
    pass


class SandboxUnavailableError(ResearchOSError):
    pass


class EvidenceStoreNotFound(ResearchOSError):
    pass


class EvidenceStoreAlreadyExists(ResearchOSError):
    pass


class EvidenceStoreRevisionConflict(ResearchOSError):
    pass


class CorruptEvidenceStore(ResearchOSError):
    pass


class EvidencePersistenceError(ResearchOSError):
    def __init__(self, message: str, *, run_id: str, store_replaced: bool) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.store_replaced = store_replaced


class EvidenceIdempotencyConflict(ResearchOSError):
    pass


class EvidenceTraceCommitError(ResearchOSError):
    def __init__(self, message: str, *, run_id: str, store_revision: int) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.store_revision = store_revision
        self.store_committed = True


class ClaimGraphNotFound(ResearchOSError):
    pass


class ClaimGraphAlreadyExists(ResearchOSError):
    pass


class ClaimGraphRevisionConflict(ResearchOSError):
    pass


class CorruptClaimGraph(ResearchOSError):
    pass


class ClaimGraphPersistenceError(ResearchOSError):
    def __init__(self, message: str, *, run_id: str, store_replaced: bool) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.store_replaced = store_replaced


class ClaimGraphTraceCommitError(ResearchOSError):
    def __init__(self, message: str, *, run_id: str, store_revision: int) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.store_revision = store_revision
        self.store_committed = True


class ClaimGraphPreconditionError(ResearchOSError):
    pass


class VerificationPreconditionError(ResearchOSError):
    pass


class VerificationModelFailure(ResearchOSError):
    pass


class VerificationCancelled(ResearchOSError):
    pass


class VerificationDeadlineExceeded(ResearchOSError):
    pass


class VerificationContractError(ResearchOSError):
    pass


class VerificationArtifactConflict(ResearchOSError):
    pass


class VerificationInputChanged(ResearchOSError):
    pass


class VerificationInputCorruption(ResearchOSError):
    """Frozen Claim/Evidence snapshots disagree structurally."""


class VerificationUsageUncertain(ResearchOSError):
    pass


class VerificationPersistenceError(ResearchOSError):
    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        authority_committed: bool,
        report_committed: bool,
        artifact_committed: bool,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.authority_committed = authority_committed
        self.report_committed = report_committed
        self.artifact_committed = artifact_committed
        # Compatibility name: the authoritative JSON is the Phase 6 artifact.
        self.artifact_replaced = authority_committed


class VerificationTraceCommitError(ResearchOSError):
    def __init__(self, message: str, *, run_id: str, verification_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.verification_id = verification_id
        self.artifact_committed = True


class EvaluationDatasetNotFound(ResearchOSError):
    pass


class EvaluationDatasetCompatibilityError(ResearchOSError):
    pass


class CorruptEvaluationDataset(ResearchOSError):
    pass


class EvaluationPreconditionError(ResearchOSError):
    pass


class CaseRunCompatibilityError(EvaluationPreconditionError):
    pass


class SUTHomogeneityError(EvaluationPreconditionError):
    pass


class EvaluationInputChanged(EvaluationPreconditionError):
    pass


class CorruptEvaluationInput(ResearchOSError):
    pass


class EvaluationContractError(ResearchOSError):
    pass


class EvaluationCancelled(ResearchOSError):
    pass


class EvaluationDeadlineExceeded(ResearchOSError):
    pass


class EvaluationModelFailure(ResearchOSError):
    pass


class EvaluationArtifactNotFound(ResearchOSError):
    pass


class CorruptEvaluationArtifact(ResearchOSError):
    pass


class EvaluationArtifactConflict(ResearchOSError):
    pass


class EvaluationPersistenceError(ResearchOSError):
    def __init__(
        self, message: str, *, artifact_id: str, authority_replaced: bool
    ) -> None:
        super().__init__(message)
        self.artifact_id = artifact_id
        self.authority_replaced = authority_replaced


class MetricDefinitionCompatibilityError(ResearchOSError):
    pass


class EvaluationProvenanceError(ResearchOSError):
    pass


class IncompatibleEvaluationRuns(ResearchOSError):
    pass


class EvaluationComparisonError(ResearchOSError):
    pass
