"""Deterministic Phase 10 structural self-check materialization."""

from __future__ import annotations

from datetime import timedelta

from researchos.adapters.evaluation_dataset import InMemoryEvaluationDatasetLoader
from researchos.domain.contracts import RunState
from researchos.domain.evaluation import (
    CaseRunBinding,
    DatasetProvenance,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRequest,
    ReferenceLevel,
)
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.workflow import Phase10WorkflowProfileV1
from researchos.interfaces.lifecycle import Clock


def structural_selfcheck_dataset(
    state: RunState, profile: Phase10WorkflowProfileV1
) -> EvaluationDataset:
    """Rebuild the one-case Phase 10 self-check solely from durable inputs."""

    identity = [
        "phase10-selfcheck-v1",
        state.run_id,
        state.input_hash,
        profile.profile_hash,
    ]
    case_id = stable_id("evalcase", identity)
    dataset_id = stable_id("evalds", identity)
    dataset = EvaluationDataset.model_construct(
        dataset_id=dataset_id,
        dataset_version="1",
        provenance=DatasetProvenance(
            license_id="researchos_internal",
            source_uri_hash=stable_hash(
                [
                    "phase10-structural-selfcheck-v1",
                    state.input_hash,
                    profile.profile_hash,
                ]
            ),
            curator_id="researchos",
            provenance_version="1",
        ),
        cases=(
            EvaluationCase(
                case_id=case_id,
                reference_level=ReferenceLevel.STRUCTURAL_ONLY,
                query=state.input_snapshot.query,
            ),
        ),
        dataset_content_hash="0" * 64,
    )
    raw = dataset.model_dump(mode="python")
    raw["dataset_content_hash"] = stable_hash(
        dataset.model_dump(mode="json", exclude={"dataset_content_hash"})
    )
    return EvaluationDataset.model_validate(raw)


def structural_selfcheck_request(
    state: RunState,
    profile: Phase10WorkflowProfileV1,
    *,
    clock: Clock,
) -> EvaluationRequest:
    dataset = structural_selfcheck_dataset(state, profile)
    return EvaluationRequest(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_hash=dataset.dataset_content_hash,
        bindings=(
            CaseRunBinding(case_id=dataset.cases[0].case_id, run_id=state.run_id),
        ),
        commit_sha=profile.system_commit_sha,
        system_version=profile.system_version,
        policy=profile.evaluation.evaluation_policy,
        deadline=clock.now()
        + timedelta(
            milliseconds=profile.evaluation.evaluation_policy.max_duration_milliseconds
        ),
    )


def structural_selfcheck_loader(
    state: RunState, profile: Phase10WorkflowProfileV1
) -> InMemoryEvaluationDatasetLoader:
    """Provide the reconstructed input through the unchanged Phase 7 port."""

    dataset = structural_selfcheck_dataset(state, profile)
    return InMemoryEvaluationDatasetLoader((dataset,))
