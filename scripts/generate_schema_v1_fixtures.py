"""Explicit maintenance tool for the checked-in schema-v1 compatibility corpus.

Never call this from pytest or CI. Updating these bytes is an intentional
release-compatibility decision and must be reviewed with its generated diff.
"""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    tests = root / "tests"
    sys.path[:0] = [str(tests), str(root)]

    from conftest import SequentialIds
    from phase9_fixtures import FrozenClock, make_settings, make_snapshot
    from runtime_fixtures import build_executor, runtime_example
    from test_phase5_evidence_claims import NeverCancelled
    from test_phase6_synthesis_verification import build_service, prepared

    from researchos.adapters.checkpoint_filesystem import FilesystemCheckpointStore
    from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
    from researchos.adapters.evaluation_dataset import (
        FilesystemEvaluationDatasetLoader,
    )
    from researchos.adapters.evaluation_filesystem import (
        FilesystemEvaluationArtifactStore,
    )
    from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
    from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
    from researchos.adapters.mock_execution import (
        MockTaskExecutionBackend,
    )
    from researchos.adapters.mock_verification import MockVerificationModel
    from researchos.adapters.real_composition_filesystem import (
        FilesystemRealCompositionStore,
    )
    from researchos.adapters.verification_filesystem import (
        FilesystemVerificationArtifactStore,
    )
    from researchos.application.checkpoint_manager import CheckpointManager
    from researchos.application.evaluation_artifacts import ReadOnlyRunArtifactReader
    from researchos.application.evaluation_evaluators import (
        DeterministicArtifactEvaluator,
    )
    from researchos.application.evaluation_harness import EvaluationHarness
    from researchos.application.run_manager import RunManager
    from researchos.application.verification_publisher import render_markdown
    from researchos.domain.contracts import (
        RunConfig,
        RunInput,
        RunStatus,
        canonical_json_bytes,
    )
    from researchos.domain.evaluation import (
        CaseRunBinding,
        DatasetProvenance,
        EvaluationCase,
        EvaluationDataset,
        EvaluationPolicy,
        EvaluationRequest,
        ReferenceLevel,
    )
    from researchos.domain.identity import stable_hash
    from researchos.domain.runtime import RuntimeResourceAmount

    output = root / "tests" / "fixtures" / "schema_v1"
    if output.exists():
        raise SystemExit("refusing to overwrite schema_v1 fixture corpus")
    output.mkdir(parents=True)

    # Lifecycle/trace: use the normal manager; fixture generation is the only
    # permitted writer and tests subsequently only read these frozen bytes.
    clock = FrozenClock()
    manager = RunManager(
        store=FilesystemRunStore(output),
        trace_sink=FilesystemTraceSink(output),
        clock=clock,
        id_factory=SequentialIds(),
    )
    state = manager.create(
        RunInput(query="runtime query"),
        RunConfig(allowed_capability_ids=("search", "read")),
    )
    for status in (RunStatus.PLANNING, RunStatus.READY, RunStatus.RUNNING):
        state = manager.transition(state.run_id, status)

    # Runtime checkpoint: construct and persist a genuine initialized v1
    # checkpoint using the Phase 3 public executor boundary.
    example = runtime_example()
    checkpoint_store = FilesystemCheckpointStore(output)
    checkpoint_manager = CheckpointManager(
        store=checkpoint_store,
        trace_sink=FilesystemTraceSink(output),
        clock=clock,
        id_factory=SequentialIds(),
    )
    # Its deterministic fixture identity is run_1, matching the lifecycle ID.
    executor, _, _, _, _ = build_executor(example, MockTaskExecutionBackend({}))
    del executor
    policies = example.policy
    from researchos.adapters.sleeper import (
        AsyncioRunCancellationController,
        ControlledSleeper,
    )
    from researchos.application.async_dag_executor import AsyncDAGExecutor

    real_executor = AsyncDAGExecutor(
        checkpoints=checkpoint_manager,
        backend=MockTaskExecutionBackend({}),
        clock=clock,
        sleeper=ControlledSleeper(),
        cancellation=AsyncioRunCancellationController(),
        id_factory=SequentialIds(),
    )
    real_executor.initialize(
        state, example.dag, policies, replan_context=example.context
    )

    # Use the deterministic Phase 5/6 builders only in this explicit fixture
    # maintenance tool. Pytest never invokes it or regenerates the bytes.
    verifying_state, policy, _, evidence, claims, fixtures = prepared()
    assert verifying_state.run_id == state.run_id
    evidence_store = FilesystemEvidenceStore(output)
    claim_store = FilesystemClaimGraphStore(output)
    evidence_store.create(evidence.load(state.run_id))
    claim_store.create(claims.load(state.run_id))
    from researchos.adapters.verification_memory import (
        InMemoryVerificationArtifactStore,
    )

    service, _ = build_service(
        evidence,
        claims,
        MockVerificationModel(fixtures),
        InMemoryVerificationArtifactStore(),
    )
    result = asyncio.run(
        service.verify(
            run_state=verifying_state,
            policy=policy,
            hard_limits=RuntimeResourceAmount(
                duration_milliseconds=10_000,
                tokens=1_000,
                cost_microunits=1_000,
                tool_calls=10,
            ),
            cancellation=NeverCancelled(),
        )
    )
    markdown = render_markdown(
        result.final_draft,
        result.judge_decisions,
        findings=result.findings,
        resolved_finding_ids=result.resolved_finding_ids,
        citation_issues=result.citation_issues,
        disposition=result.disposition,
        acquisition_requests=result.acquisition_requests,
        omitted_claim_ids=result.omitted_claim_ids,
    )
    FilesystemVerificationArtifactStore(output).publish(
        result, markdown, expected_prior_verification_id=None
    )
    FilesystemRealCompositionStore(output, clock=clock).create(
        make_snapshot(
            make_settings(), run_id=state.run_id, config_hash=state.config_hash
        )
    )

    # Phase 7 authority: a checked-in dataset plus the separately persisted
    # case and evaluation authority. The fixture Run intentionally remains
    # nonterminal, so the explicit policy models the frozen compatibility path
    # rather than changing RunState directly.
    case = EvaluationCase(
        case_id="schema_v1_case",
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="runtime query",
    )
    dataset_values = {
        "dataset_id": "schema_v1_eval",
        "dataset_version": "1",
        "provenance": DatasetProvenance(
            license_id="schema_v1_fixture",
            source_uri_hash="0" * 64,
            curator_id="researchos",
            provenance_version="1",
        ),
        "cases": (case,),
    }
    provisional_dataset = EvaluationDataset.model_construct(
        **dataset_values, dataset_content_hash="0" * 64
    )
    dataset = EvaluationDataset(
        **dataset_values,
        dataset_content_hash=stable_hash(
            provisional_dataset.model_dump(
                mode="json", exclude={"dataset_content_hash"}
            )
        ),
    )
    dataset_path = output / "evaluation_datasets" / dataset.dataset_id / "1.json"
    dataset_path.parent.mkdir(parents=True)
    dataset_path.write_bytes(canonical_json_bytes(dataset) + b"\n")
    evaluation_policy = EvaluationPolicy(
        policy_id="schema_v1_eval_policy",
        policy_version="1",
        enabled_evaluator_ids=("deterministic_core",),
        allow_nonterminal_runs=True,
    )
    evaluation = asyncio.run(
        EvaluationHarness(
            datasets=FilesystemEvaluationDatasetLoader(
                output / "evaluation_datasets", max_dataset_bytes=1_000_000
            ),
            artifacts=FilesystemEvaluationArtifactStore(output / "evaluations"),
            run_artifacts=ReadOnlyRunArtifactReader(
                output, max_input_bytes_per_case=1_000_000
            ),
            clock=clock,
            evaluators=(DeterministicArtifactEvaluator(),),
        ).evaluate_existing(
            EvaluationRequest(
                dataset_id=dataset.dataset_id,
                dataset_version=dataset.dataset_version,
                dataset_hash=dataset.dataset_content_hash,
                bindings=(CaseRunBinding(case_id=case.case_id, run_id=state.run_id),),
                commit_sha="a" * 40,
                system_version="schema_v1_fixture",
                policy=evaluation_policy,
                deadline=clock.now() + timedelta(minutes=1),
            ),
            NeverCancelled(),
        )
    )
    assert evaluation.cases
    (output / "CORPUS_README.txt").write_text(
        "Schema-v1 frozen release corpus. Regenerate only with this explicit maintenance tool.\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
