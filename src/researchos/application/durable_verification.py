"""Lifecycle-only durable integration around the existing Phase 6 service."""

from __future__ import annotations

from contextlib import suppress

from pydantic import ValidationError

from researchos.application.checkpoint_manager import CheckpointManager
from researchos.application.errors import (
    CheckpointNotFound,
    CorruptCheckpoint,
    VerificationCancelled,
    VerificationContractError,
    VerificationDeadlineExceeded,
    VerificationOperationAlreadyExists,
    VerificationOperationNotFound,
    VerificationPreconditionError,
    VerificationRecoveryInconsistency,
)
from researchos.application.observation_recorder import ObservationRecorder
from researchos.application.run_manager import RunManager
from researchos.application.verification_lineage import (
    reconstruct_verification_lineage,
)
from researchos.application.verification_operation import (
    VerificationOperationManager,
)
from researchos.application.verification_service import VerificationService
from researchos.domain.contracts import RunState, RunStatus, model_sha256
from researchos.domain.runtime import (
    AttemptStatus,
    ExecutorStatus,
    RuntimeResourceAmount,
    TaskStatus,
    UsageCertainty,
)
from researchos.domain.synthesis import VerificationPolicy, VerificationResult
from researchos.domain.verification_runtime import (
    VerificationOperation,
    VerificationOperationStatus,
    validate_operation_capacity,
)
from researchos.interfaces.runtime import CancellationSignal
from researchos.interfaces.verification import VerificationArtifactStore


class DurableVerificationCoordinator:
    """Coordinates lifecycle only; Phase 6 still owns verification semantics."""

    def __init__(
        self,
        *,
        run_manager: RunManager,
        verification_service: VerificationService,
        operation_manager: VerificationOperationManager,
        artifact_store: VerificationArtifactStore,
        observation_recorder: ObservationRecorder,
        checkpoint_manager: CheckpointManager | None = None,
    ) -> None:
        self._runs = run_manager
        self._service = verification_service
        self._operations = operation_manager
        self._artifacts = artifact_store
        self._observations = observation_recorder
        self._checkpoints = checkpoint_manager

    async def execute(
        self,
        *,
        run_id: str,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        cancellation: CancellationSignal,
        expected_prior_verification_id: str | None = None,
    ) -> VerificationResult:
        validate_operation_capacity(policy)
        state = self._runs.load(run_id)
        authority = self._artifacts.load(run_id)
        if state.status in {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            if authority is None:
                raise VerificationRecoveryInconsistency(
                    "terminal Run lacks verification authority"
                )
            return self._recover_authority(
                state=state,
                policy=policy,
                hard_limits=hard_limits,
                authority=authority,
            )
        if state.status is RunStatus.RUNNING:
            # Capacity rejection happens before this lifecycle mutation.
            self._require_terminal_execution(state)
            state = self._runs.transition(run_id, RunStatus.VERIFYING)
        elif state.status not in {RunStatus.VERIFYING, RunStatus.EVALUATING}:
            raise VerificationPreconditionError(
                "durable verification requires RUNNING, VERIFYING, or EVALUATING"
            )

        if state.status is RunStatus.EVALUATING:
            if authority is None:
                raise VerificationRecoveryInconsistency(
                    "EVALUATING Run lacks verification authority"
                )
            return self._recover_authority(
                state=state,
                policy=policy,
                hard_limits=hard_limits,
                authority=authority,
            )

        frozen, verification_id = self._service.prepare_invocation(
            run_state=state, policy=policy
        )
        generations = self._operations.list_generations(run_id)
        existing_operation = next(
            (
                item
                for item in generations
                if item.verification_id == verification_id
            ),
            None,
        )
        durable_prior = (
            None
            if existing_operation is None
            else existing_operation.expected_prior_verification_id
        )
        if (
            expected_prior_verification_id is not None
            and durable_prior is not None
            and expected_prior_verification_id != durable_prior
        ):
            raise VerificationRecoveryInconsistency(
                "incoming predecessor differs from durable operation pin"
            )
        effective_prior = durable_prior or expected_prior_verification_id
        if authority is not None:
            if authority.verification_id == verification_id:
                return self._recover_authority(
                    state=state,
                    policy=policy,
                    hard_limits=hard_limits,
                    authority=authority,
                )
            if effective_prior is None and (
                authority.policy_hash != model_sha256(policy)
                or authority.model_bundle_hash != self._service.model_bundle_hash
            ):
                raise VerificationRecoveryInconsistency(
                    "verification authority policy/model pins differ"
                )
            if authority.verification_id != effective_prior:
                raise VerificationRecoveryInconsistency(
                    "existing verification authority is not the declared predecessor"
                )
        self._validate_generation_boundary(
            generations=generations,
            verification_id=verification_id,
            authority=authority,
            expected_prior_verification_id=effective_prior,
        )
        created = False
        if existing_operation is None:
            try:
                operation = self._operations.create(
                    frozen=frozen,
                    policy=policy,
                    hard_limits=hard_limits,
                    model_bundle_hash=self._service.model_bundle_hash,
                    verification_id=verification_id,
                    expected_prior_verification_id=effective_prior,
                )
            except VerificationOperationAlreadyExists as exc:
                raise VerificationRecoveryInconsistency(
                    "another verification generation became active"
                ) from exc
            created = True
        else:
            operation = existing_operation
        self._validate_operation(
            operation,
            state,
            frozen.frozen_input_hash,
            policy,
            hard_limits,
            expected_verification_id=verification_id,
            expected_model_bundle_hash=self._service.model_bundle_hash,
            expected_prior_verification_id=effective_prior,
        )
        if not created:
            if operation.status is VerificationOperationStatus.COMPLETED:
                raise VerificationRecoveryInconsistency(
                    "completed operation lacks verification authority"
                )
            if operation.status in {
                VerificationOperationStatus.INTERRUPTED_UNKNOWN,
                VerificationOperationStatus.FAILED,
                VerificationOperationStatus.CANCELLED,
            }:
                raise VerificationRecoveryInconsistency(
                    "verification operation is durably terminal without authority"
                )
            operation = self._operations.begin_recovery(run_id, verification_id)
            self._operations.reconcile_outbox(run_id, verification_id)
            if operation.status is VerificationOperationStatus.INTERRUPTED_UNKNOWN:
                self._operations.complete_recovery(run_id, verification_id)
                raise VerificationRecoveryInconsistency(
                    "verification operation has an unknown dispatched outcome"
                )
            if operation.status is VerificationOperationStatus.READY_TO_PUBLISH:
                assert operation.publication_candidate is not None
                return self._publish_candidate(
                    state=state,
                    frozen=frozen,
                    operation=operation,
                    recovery=True,
                )

        session = self._operations.session(run_id, verification_id)
        try:
            result = await self._service.verify(
                run_state=state,
                policy=policy,
                hard_limits=hard_limits,
                cancellation=cancellation,
                expected_prior_verification_id=effective_prior,
                frozen_input=frozen,
                call_journal=session,
                event_sink=lambda event: self._operations.record_event(
                    run_id,
                    event,
                    verification_id=verification_id,
                    critical=False,
                ),
            )
        except VerificationCancelled:
            self._operations.terminalize_predispatch(
                run_id,
                verification_id=verification_id,
                status=VerificationOperationStatus.CANCELLED,
                failure_code="verification_cancelled_before_dispatch",
            )
            raise
        except (VerificationDeadlineExceeded, VerificationContractError) as exc:
            self._operations.terminalize_predispatch(
                run_id,
                verification_id=verification_id,
                status=VerificationOperationStatus.FAILED,
                failure_code=(
                    "verification_deadline_elapsed_before_dispatch"
                    if isinstance(exc, VerificationDeadlineExceeded)
                    else "verification_predispatch_contract_failed"
                ),
            )
            raise
        except (TypeError, ValidationError):
            self._operations.terminalize_predispatch(
                run_id,
                verification_id=verification_id,
                status=VerificationOperationStatus.FAILED,
                failure_code="verification_request_contract_invalid",
            )
            raise
        current = self._operations.load(run_id, verification_id)
        if current.status is VerificationOperationStatus.EXECUTING:
            raise VerificationRecoveryInconsistency(
                "Phase 6 returned without a durable publication-ready checkpoint"
            )
        if current.publication_candidate is None or (
            current.publication_candidate.result != result
        ):
            raise VerificationRecoveryInconsistency(
                "published authority differs from durable publication candidate"
            )
        return self._publish_candidate(
            state=state,
            frozen=frozen,
            operation=current,
            recovery=not created,
        )

    async def recover(
        self,
        *,
        run_id: str,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        cancellation: CancellationSignal,
    ) -> VerificationResult:
        return await self.execute(
            run_id=run_id,
            policy=policy,
            hard_limits=hard_limits,
            cancellation=cancellation,
        )

    def _recover_authority(
        self,
        *,
        state: RunState,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        authority: VerificationResult,
    ) -> VerificationResult:
        # The store has already validated the authority schema, content hash,
        # Markdown hash, and internal verification identity before mutable input
        # is consulted.
        if (
            authority.run_id != state.run_id
            or authority.policy_hash != model_sha256(policy)
            or authority.model_bundle_hash != self._service.model_bundle_hash
        ):
            raise VerificationRecoveryInconsistency(
                "verification authority policy/model/Run pins differ"
            )
        if state.status is RunStatus.VERIFYING:
            expected_state_revision = authority.run_revision
        elif state.status is RunStatus.EVALUATING:
            expected_state_revision = authority.run_revision + 1
        else:
            expected_state_revision = authority.run_revision + 2
        if state.revision != expected_state_revision:
            raise VerificationRecoveryInconsistency(
                "verification authority Run revision differs"
            )
        frozen = self._service.freeze_input(
            run_id=authority.run_id,
            run_revision=authority.run_revision,
            policy=policy,
        )
        if (
            frozen.frozen_input_hash != authority.frozen_input_hash
            or frozen.claim_snapshot_hash != authority.claim_snapshot_hash
            or frozen.evidence_snapshot_hash != authority.evidence_snapshot_hash
        ):
            raise VerificationRecoveryInconsistency(
                "current Claim/Evidence snapshot differs from historical authority"
            )
        try:
            operation = self._operations.load(
                state.run_id, authority.verification_id
            )
        except VerificationOperationNotFound:
            operation = self._operations.bootstrap_authority(
                frozen=frozen,
                policy=policy,
                hard_limits=hard_limits,
                authority=authority,
            )
        else:
            self._validate_operation(
                operation,
                state,
                frozen.frozen_input_hash,
                policy,
                hard_limits,
                authority=authority,
                expected_prior_verification_id=(
                    authority.supersedes_verification_id
                ),
            )
            if operation.status is VerificationOperationStatus.COMPLETED:
                with suppress(Exception):
                    self._operations.reconcile_outbox(
                        state.run_id, authority.verification_id
                    )
                self._artifacts.reconcile_report(state.run_id)
                return authority
            operation = self._operations.begin_recovery(
                state.run_id, authority.verification_id
            )
            self._operations.reconcile_outbox(
                state.run_id, authority.verification_id
            )
            if operation.publication_candidate is None:
                markdown = self._render_authority(authority)
                operation = self._operations.mark_ready_to_publish(
                    state.run_id,
                    authority,
                    markdown,
                    expected_prior_verification_id=(
                        authority.supersedes_verification_id
                    ),
                    verification_id=authority.verification_id,
                )
        return self._publish_candidate(
            state=state,
            frozen=frozen,
            operation=operation,
            recovery=operation.recovery_attempts > 0,
        )

    def _publish_candidate(
        self,
        *,
        state: RunState,
        frozen,
        operation: VerificationOperation,
        recovery: bool,
    ) -> VerificationResult:
        candidate = operation.publication_candidate
        if candidate is None:
            raise VerificationRecoveryInconsistency(
                "publication-ready operation lacks immutable candidate"
            )
        markdown = candidate.markdown_text.encode("utf-8")
        self._artifacts.publish(
            candidate.result,
            markdown,
            expected_prior_verification_id=(
                candidate.expected_prior_verification_id
            ),
        )
        authority = self._artifacts.load(state.run_id)
        if authority != candidate.result:
            raise VerificationRecoveryInconsistency(
                "published authority differs from immutable candidate"
            )
        if operation.lineage is None:
            operation = self._operations.record_lineage(
                state.run_id,
                reconstruct_verification_lineage(authority, frozen),
                verification_id=operation.verification_id,
            )
        if operation.status in {
            VerificationOperationStatus.READY_TO_PUBLISH,
            VerificationOperationStatus.CANCELLED,
            VerificationOperationStatus.INTERRUPTED_UNKNOWN,
        }:
            operation = self._operations.advance_status(
                state.run_id,
                VerificationOperationStatus.AUTHORITY_COMMITTED,
                verification_id=operation.verification_id,
                authority=authority,
            )
        self._operations.flush_optional_omission_summary(
            state.run_id, operation.verification_id
        )
        if state.status is RunStatus.VERIFYING:
            state = self._runs.transition(state.run_id, RunStatus.EVALUATING)
        if operation.status is VerificationOperationStatus.AUTHORITY_COMMITTED:
            operation = self._operations.advance_status(
                state.run_id,
                VerificationOperationStatus.LIFECYCLE_COMMITTED,
                verification_id=operation.verification_id,
            )
        if operation.status is VerificationOperationStatus.LIFECYCLE_COMMITTED:
            operation = self._operations.advance_status(
                state.run_id,
                VerificationOperationStatus.COMPLETED,
                verification_id=operation.verification_id,
            )
        if recovery:
            self._operations.complete_recovery(
                state.run_id, operation.verification_id
            )
        return authority

    @staticmethod
    def _validate_operation(
        operation: VerificationOperation,
        state: RunState,
        frozen_input_hash: str,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        *,
        authority: VerificationResult | None = None,
        expected_verification_id: str | None = None,
        expected_model_bundle_hash: str | None = None,
        expected_prior_verification_id: str | None = None,
    ) -> None:
        if authority is not None:
            allowed_revision = authority.run_revision
        else:
            allowed_revision = state.revision
        if (
            operation.run_id != state.run_id
            or operation.run_revision != allowed_revision
            or operation.frozen_input_hash != frozen_input_hash
            or operation.policy_hash != model_sha256(policy)
            or operation.hard_limits_hash != model_sha256(hard_limits)
            or operation.expected_prior_verification_id
            != expected_prior_verification_id
            or (
                expected_verification_id is not None
                and operation.verification_id != expected_verification_id
            )
            or (
                expected_model_bundle_hash is not None
                and operation.model_bundle_hash != expected_model_bundle_hash
            )
        ):
            raise VerificationRecoveryInconsistency(
                "verification operation compatibility pins differ"
            )
        if authority is not None and (
            operation.verification_id != authority.verification_id
            or (
                operation.authority_content_hash is not None
                and operation.authority_content_hash
                != authority.artifact_content_hash
            )
            or (
                operation.publication_candidate is not None
                and operation.publication_candidate.result != authority
            )
        ):
            raise VerificationRecoveryInconsistency(
                "operation and verification authority differ"
            )

    def _require_terminal_execution(self, state: RunState) -> None:
        if self._checkpoints is None:
            raise VerificationPreconditionError(
                "Phase 3 checkpoint authority is required before VERIFYING"
            )
        try:
            checkpoint = self._checkpoints.load_and_reconcile(state.run_id)
        except CheckpointNotFound as exc:
            raise VerificationPreconditionError(
                "Phase 3 checkpoint is missing"
            ) from exc
        except CorruptCheckpoint as exc:
            raise VerificationRecoveryInconsistency(
                "Phase 3 checkpoint is corrupt"
            ) from exc
        if (
            checkpoint.run_id != state.run_id
            or checkpoint.run_revision != state.revision
            or checkpoint.dag.run_id != state.run_id
            or checkpoint.dag_id != checkpoint.dag.dag_id
            or checkpoint.plan_id != checkpoint.dag.plan_id
            or checkpoint.dag_hash != model_sha256(checkpoint.dag)
        ):
            raise VerificationRecoveryInconsistency(
                "Phase 3 checkpoint Run/revision/DAG identity differs"
            )
        zero = RuntimeResourceAmount()
        unsafe_attempt = any(
            attempt.status in {AttemptStatus.RUNNING, AttemptStatus.INTERRUPTED}
            or (
                attempt.status is not AttemptStatus.RUNNING
                and attempt.usage_certainty is UsageCertainty.UNKNOWN
            )
            for task in checkpoint.task_states
            for attempt in task.attempts
        )
        if (
            checkpoint.executor_status is not ExecutorStatus.COMPLETED
            or any(
                task.status in {TaskStatus.RUNNING, TaskStatus.RETRY_WAIT}
                for task in checkpoint.task_states
            )
            or unsafe_attempt
            or checkpoint.budget.reserved != zero
            or checkpoint.budget.uncertain_consumption != zero
            or checkpoint.replan.pending_request is not None
            or checkpoint.cancellation_requested_at is not None
        ):
            raise VerificationPreconditionError(
                "Phase 3 execution is not safely terminal"
            )

    @staticmethod
    def _validate_generation_boundary(
        *,
        generations: tuple[VerificationOperation, ...],
        verification_id: str,
        authority: VerificationResult | None,
        expected_prior_verification_id: str | None,
    ) -> None:
        other = tuple(
            item for item in generations if item.verification_id != verification_id
        )
        if not other:
            return
        if any(
            item.status is not VerificationOperationStatus.COMPLETED
            for item in other
        ):
            raise VerificationRecoveryInconsistency(
                "unfinished verification generation must be recovered first"
            )
        if (
            authority is None
            or expected_prior_verification_id != authority.verification_id
            or not any(
                item.verification_id == authority.verification_id
                and item.authority_content_hash == authority.artifact_content_hash
                and item.publication_candidate is not None
                and item.publication_candidate.result == authority
                for item in other
            )
        ):
            raise VerificationRecoveryInconsistency(
                "completed verification generation is not an authoritative predecessor"
            )

    @staticmethod
    def _render_authority(authority: VerificationResult) -> bytes:
        from researchos.application.verification_publisher import render_markdown

        return render_markdown(
            authority.final_draft,
            authority.judge_decisions,
            findings=authority.findings,
            resolved_finding_ids=authority.resolved_finding_ids,
            citation_issues=authority.citation_issues,
            disposition=authority.disposition,
            acquisition_requests=authority.acquisition_requests,
            omitted_claim_ids=authority.omitted_claim_ids,
        )
