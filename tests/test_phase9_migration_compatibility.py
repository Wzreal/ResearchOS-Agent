"""Read-only compatibility checks for the checked-in schema-v1 corpus."""

# ruff: noqa: E501

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from phase9_fixtures import FrozenClock

from researchos.adapters.checkpoint_filesystem import FilesystemCheckpointStore
from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
from researchos.adapters.evaluation_dataset import FilesystemEvaluationDatasetLoader
from researchos.adapters.evaluation_filesystem import FilesystemEvaluationArtifactStore
from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.adapters.real_composition_filesystem import (
    FilesystemRealCompositionStore,
)
from researchos.adapters.verification_filesystem import (
    FilesystemVerificationArtifactStore,
)
from researchos.application.errors import (
    CheckpointCompatibilityError,
    CorruptEvaluationArtifact,
    CorruptEvaluationDataset,
    CorruptRunState,
    IncompatibleSchema,
    RealCompositionCorruption,
)
from researchos.domain.contracts import canonical_json_bytes

CORPUS = Path(__file__).parent / "fixtures" / "schema_v1"
RUN_ID = "run_1"


def test_schema_v1_checked_in_corpus_loads_with_fresh_adapters():
    """The baseline is read as committed bytes; pytest never regenerates it."""

    state = FilesystemRunStore(CORPUS).load(RUN_ID)
    trace = FilesystemTraceSink(CORPUS).read(RUN_ID)
    checkpoint = FilesystemCheckpointStore(CORPUS).load(RUN_ID)
    evidence = FilesystemEvidenceStore(CORPUS).load(RUN_ID)
    claims = FilesystemClaimGraphStore(CORPUS).load(RUN_ID)
    composition = FilesystemRealCompositionStore(CORPUS, clock=FrozenClock()).load(
        RUN_ID
    )
    verification = FilesystemVerificationArtifactStore(CORPUS).load(RUN_ID)
    dataset_root = CORPUS / "evaluation_datasets"
    dataset = FilesystemEvaluationDatasetLoader(
        dataset_root, max_dataset_bytes=1_000_000
    ).load("schema_v1_eval", "1")
    evaluation_path = next((CORPUS / "evaluations").glob("*/evaluation.json"))
    evaluation = FilesystemEvaluationArtifactStore(CORPUS / "evaluations").load(
        evaluation_path.parent.name
    )

    assert state.run_id == checkpoint.run_id == composition.snapshot.semantic.run_id
    assert trace and all(event.run_id == RUN_ID for event in trace)
    assert evidence.run_id == claims.run_id == RUN_ID
    assert verification is not None and verification.run_id == RUN_ID
    assert evaluation is not None and evaluation.cases[0].run_id == RUN_ID
    assert (CORPUS / RUN_ID / "run_state.json").read_bytes() == (
        canonical_json_bytes(state) + b"\n"
    )
    assert (dataset_root / "schema_v1_eval" / "1.json").read_bytes() == (
        canonical_json_bytes(dataset) + b"\n"
    )
    assert evaluation_path.read_bytes() == canonical_json_bytes(evaluation) + b"\n"
    case = evaluation.cases[0]
    case_path = (
        evaluation_path.parent
        / "cases"
        / f"{case.case_id}.{case.artifact_content_hash[:24]}.json"
    )
    assert case_path.read_bytes() == canonical_json_bytes(case) + b"\n"


def test_schema_v1_unknown_schema_tamper_and_torn_trace_fail_closed(tmp_path):
    copied = tmp_path / "schema_v1"
    shutil.copytree(CORPUS, copied)
    state_path = copied / RUN_ID / "run_state.json"
    state_path.write_bytes(
        state_path.read_bytes().replace(
            b'"schema_version":1', b'"schema_version":999', 1
        )
    )
    with pytest.raises(
        (CheckpointCompatibilityError, CorruptRunState, IncompatibleSchema)
    ):
        FilesystemRunStore(copied).load(RUN_ID)

    shutil.rmtree(copied)
    shutil.copytree(CORPUS, copied)
    trace_path = copied / RUN_ID / "trace.jsonl"
    trace_path.write_bytes(trace_path.read_bytes()[:-7])
    with pytest.raises(CorruptRunState):
        FilesystemTraceSink(copied).read(RUN_ID)

    shutil.rmtree(copied)
    shutil.copytree(CORPUS, copied)
    composition_path = copied / RUN_ID / "real_composition.json"
    composition_path.write_bytes(composition_path.read_bytes()[:-1] + b" ")
    with pytest.raises(RealCompositionCorruption):
        FilesystemRealCompositionStore(copied, clock=FrozenClock()).load(RUN_ID)

    shutil.rmtree(copied)
    shutil.copytree(CORPUS, copied)
    dataset_path = copied / "evaluation_datasets" / "schema_v1_eval" / "1.json"
    dataset_path.write_bytes(
        dataset_path.read_bytes().replace(
            b'"schema_version":1', b'"schema_version":999', 1
        )
    )
    with pytest.raises(CorruptEvaluationDataset):
        FilesystemEvaluationDatasetLoader(
            copied / "evaluation_datasets", max_dataset_bytes=1_000_000
        ).load("schema_v1_eval", "1")

    shutil.rmtree(copied)
    shutil.copytree(CORPUS, copied)
    evaluation_path = next((copied / "evaluations").glob("*/evaluation.json"))
    evaluation_path.write_bytes(
        evaluation_path.read_bytes().replace(
            b'"artifact_content_hash":"', b'"artifact_content_hash":"f', 1
        )
    )
    with pytest.raises(CorruptEvaluationArtifact):
        FilesystemEvaluationArtifactStore(copied / "evaluations").load(
            evaluation_path.parent.name
        )
