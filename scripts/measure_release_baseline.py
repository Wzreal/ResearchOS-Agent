"""Read and publish the deterministic schema-v1 semantic baseline.

This tool is deliberately offline and read-only with respect to the fixture
corpus.  It validates the checked-in bytes through fresh filesystem adapters
and writes one non-authoritative measurement occurrence to an operator-chosen
path.  The occurrence metadata is not suitable for performance comparison.
"""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.domain.runtime import CheckpointEnvelope

MEASUREMENT_SCHEMA_VERSION = 1
MEASUREMENT_KIND = "deterministic_offline_schema_v1_semantic_baseline"
RUN_ID = "run_1"
_REQUIRED_ARTIFACTS = (
    "run_1/run_state.json",
    "run_1/trace.jsonl",
    "run_1/checkpoint.json",
    "run_1/evidence.jsonl",
    "run_1/claims.jsonl",
    "run_1/verification.json",
    "run_1/report.md",
    "run_1/real_composition.json",
    "evaluation_datasets/schema_v1_eval/1.json",
)


class _FixtureClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 31, tzinfo=UTC)


def _canonical_value_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_canonical_json(
    path: Path, model: Any, *, trailing_newline: bool = True
) -> None:
    expected = canonical_json_bytes(model)
    if trailing_newline:
        expected += b"\n"
    if path.read_bytes() != expected:
        raise ValueError(f"canonical bytes differ: {path.as_posix()}")


def _metric_projection(metric: Any) -> dict[str, Any]:
    raw = metric.model_dump(mode="json")
    result = {
        "metric_id": raw["metric_id"],
        "definition_hash": raw["definition_hash"],
        "status": raw["status"],
        "value_type": raw["value_type"],
    }
    for field in ("certainty", "value", "numerator", "denominator", "reason_code"):
        if field in raw and raw[field] is not None:
            result[field] = raw[field]
    return result


def collect_semantic_baseline(corpus: Path) -> dict[str, Any]:
    """Load a frozen v1 corpus and return its machine-independent projection."""

    supplied_corpus = Path(corpus)
    if supplied_corpus.is_symlink():
        raise ValueError("schema-v1 corpus must not be a symbolic link")
    corpus = supplied_corpus.resolve()
    if not corpus.is_dir():
        raise ValueError("schema-v1 corpus directory is absent")

    paths = [corpus / relative for relative in _REQUIRED_ARTIFACTS]
    missing = [
        path.relative_to(corpus).as_posix() for path in paths if not path.is_file()
    ]
    if missing:
        raise ValueError(f"schema-v1 corpus is missing required artifacts: {missing}")
    if any(path.is_symlink() for path in paths):
        raise ValueError("schema-v1 artifacts must not be symbolic links")

    run_store = FilesystemRunStore(corpus)
    trace_sink = FilesystemTraceSink(corpus)
    checkpoint_store = FilesystemCheckpointStore(corpus)
    evidence_store = FilesystemEvidenceStore(corpus)
    claim_store = FilesystemClaimGraphStore(corpus)
    verification_store = FilesystemVerificationArtifactStore(corpus)
    composition_store = FilesystemRealCompositionStore(corpus, clock=_FixtureClock())

    state = run_store.load(RUN_ID)
    trace = trace_sink.read(RUN_ID)
    checkpoint = checkpoint_store.load(RUN_ID)
    evidence = evidence_store.load(RUN_ID)
    claims = claim_store.load(RUN_ID)
    verification = verification_store.load(RUN_ID)
    composition = composition_store.load(RUN_ID)
    if verification is None:
        raise ValueError("schema-v1 verification authority is absent")

    dataset_path = corpus / "evaluation_datasets" / "schema_v1_eval" / "1.json"
    dataset = FilesystemEvaluationDatasetLoader(
        corpus / "evaluation_datasets", max_dataset_bytes=1_000_000
    ).load("schema_v1_eval", "1")
    evaluation_paths = sorted((corpus / "evaluations").glob("*/evaluation.json"))
    if len(evaluation_paths) != 1:
        raise ValueError(
            "schema-v1 corpus must contain exactly one evaluation authority"
        )
    evaluation_path = evaluation_paths[0]
    evaluation = FilesystemEvaluationArtifactStore(evaluation_path.parent.parent).load(
        evaluation_path.parent.name
    )
    if evaluation is None:
        raise ValueError("schema-v1 evaluation authority is absent")

    _require_canonical_json(corpus / "run_1" / "run_state.json", state)
    trace_path = corpus / "run_1" / "trace.jsonl"
    expected_trace = b"".join(canonical_json_bytes(event) + b"\n" for event in trace)
    if trace_path.read_bytes() != expected_trace:
        raise ValueError("canonical trace bytes differ")
    checkpoint_path = corpus / "run_1" / "checkpoint.json"
    checkpoint_envelope = CheckpointEnvelope.model_validate_json(
        checkpoint_path.read_bytes()
    )
    if checkpoint_envelope.checkpoint != checkpoint:
        raise ValueError("checkpoint envelope differs from checkpoint store result")
    _require_canonical_json(checkpoint_path, checkpoint_envelope)
    _require_canonical_json(
        corpus / "run_1" / "verification.json",
        verification,
        trailing_newline=False,
    )
    _require_canonical_json(
        corpus / "run_1" / "real_composition.json", composition
    )
    _require_canonical_json(dataset_path, dataset)
    _require_canonical_json(evaluation_path, evaluation)
    for case in evaluation.cases:
        case_path = (
            evaluation_path.parent
            / "cases"
            / f"{case.case_id}.{case.artifact_content_hash[:24]}.json"
        )
        _require_canonical_json(case_path, case)
        paths.append(case_path)

    report_path = corpus / "run_1" / "report.md"
    if _sha256(report_path.read_bytes()) != verification.markdown_sha256:
        raise ValueError("verification report bytes differ from markdown hash")

    artifact_files = tuple(
        sorted(
            {
                *paths,
                evaluation_path,
                *(evaluation_path.parent / "cases" / f"{case.case_id}.{case.artifact_content_hash[:24]}.json" for case in evaluation.cases),
            },
            key=lambda item: item.relative_to(corpus).as_posix(),
        )
    )
    artifacts = [
        {
            "path": path.relative_to(corpus).as_posix(),
            "byte_count": len(path.read_bytes()),
            "sha256": _sha256(path.read_bytes()),
        }
        for path in artifact_files
    ]

    cases = [
        {
            "case_id": case.case_id,
            "run_id": case.run_id,
            "case_semantic_hash": case.case_semantic_hash,
            "artifact_content_hash": case.artifact_content_hash,
            "metrics": [
                _metric_projection(metric)
                for metric in sorted(case.metrics, key=lambda item: item.metric_id)
            ],
        }
        for case in sorted(evaluation.cases, key=lambda item: item.case_id)
    ]
    return {
        "measurement_schema_version": MEASUREMENT_SCHEMA_VERSION,
        "measurement_kind": MEASUREMENT_KIND,
        "corpus_schema": "schema_v1",
        "artifact_file_count": len(artifacts),
        "artifact_total_bytes": sum(item["byte_count"] for item in artifacts),
        "artifacts": artifacts,
        "authorities": {
            "run": {
                "run_id": state.run_id,
                "run_revision": state.revision,
                "run_status": state.status.value,
                "input_hash": state.input_hash,
                "config_hash": state.config_hash,
            },
            "trace": {
                "run_id": state.run_id,
                "event_count": len(trace),
                "event_ids": [event.event_id for event in trace],
            },
            "checkpoint": {
                "run_id": checkpoint.run_id,
                "checkpoint_revision": checkpoint.checkpoint_revision,
                "dag_id": checkpoint.dag_id,
                "dag_hash": checkpoint.dag_hash,
                "execution_policy_hash": checkpoint.execution_policy_hash,
                "payload_sha256": checkpoint_envelope.payload_sha256,
            },
            "evidence": {
                "run_id": evidence.run_id,
                "store_revision": evidence.store_revision,
                "snapshot_hash": model_sha256(evidence),
                "source_count": len(evidence.sources),
                "evidence_count": len(evidence.evidence),
                "revision_count": len(evidence.revisions),
                "receipt_count": len(evidence.receipts),
            },
            "claims": {
                "run_id": claims.run_id,
                "store_revision": claims.store_revision,
                "snapshot_hash": model_sha256(claims),
                "claim_count": len(claims.claims),
                "claim_revision_count": len(claims.claim_revisions),
                "edge_count": len(claims.edges),
                "edge_revision_count": len(claims.edge_revisions),
                "receipt_count": len(claims.receipts),
            },
            "verification": {
                "run_id": verification.run_id,
                "verification_id": verification.verification_id,
                "synthesis_id": verification.synthesis_id,
                "artifact_content_hash": verification.artifact_content_hash,
                "frozen_input_hash": verification.frozen_input_hash,
                "markdown_sha256": verification.markdown_sha256,
                "disposition": verification.disposition.value,
            },
            "real_composition": {
                "run_id": composition.snapshot.semantic.run_id,
                "composition_id": composition.snapshot.composition_id,
                "composition_hash": composition.snapshot.composition_hash,
                "artifact_content_hash": composition.artifact_content_hash,
            },
            "evaluation_dataset": {
                "dataset_id": dataset.dataset_id,
                "dataset_version": dataset.dataset_version,
                "dataset_content_hash": dataset.dataset_content_hash,
            },
            "evaluation": {
                "eval_run_id": evaluation.eval_run_id,
                "status": evaluation.status.value,
                "evaluation_semantic_hash": evaluation.evaluation_semantic_hash,
                "artifact_content_hash": evaluation.artifact_content_hash,
                "evaluator_bundle_hash": evaluation.evaluator_bundle_hash,
                "evaluation_policy_hash": evaluation.evaluation_policy_hash,
            },
        },
        "evaluation_cases": cases,
    }


def render_published_baseline(baseline: dict[str, Any]) -> str:
    """Render only stable fixture facts for the checked-in results document."""

    authorities = baseline["authorities"]
    lines = [
        "<!-- BEGIN schema-v1 semantic baseline -->",
        "### Frozen schema-v1 semantic baseline",
        "",
        "Classification: **DETERMINISTIC_OFFLINE**. These are fixture and authority",
        "facts, not REAL quality, latency, throughput, or cost benchmarks.",
        "",
        f"- Artifact files: `{baseline['artifact_file_count']}`",
        f"- Artifact bytes: `{baseline['artifact_total_bytes']}`",
        "- Corpus hash convention: SHA-256 of checked-in canonical persisted bytes;",
        "  the corpus is Git binary/no-EOL-conversion data.",
        "",
        "| Authority | Stable identity / hash |",
        "| --- | --- |",
        f"| Run | `{authorities['run']['run_id']}` rev `{authorities['run']['run_revision']}`; input `{authorities['run']['input_hash']}`; config `{authorities['run']['config_hash']}` |",
        f"| Checkpoint | `{authorities['checkpoint']['dag_id']}`; DAG `{authorities['checkpoint']['dag_hash']}`; payload `{authorities['checkpoint']['payload_sha256']}` |",
        f"| Evidence | rev `{authorities['evidence']['store_revision']}`; snapshot `{authorities['evidence']['snapshot_hash']}` |",
        f"| Claims | rev `{authorities['claims']['store_revision']}`; snapshot `{authorities['claims']['snapshot_hash']}` |",
        f"| Verification | `{authorities['verification']['verification_id']}`; authority `{authorities['verification']['artifact_content_hash']}`; report `{authorities['verification']['markdown_sha256']}` |",
        f"| REAL composition | `{authorities['real_composition']['composition_id']}`; semantic `{authorities['real_composition']['composition_hash']}`; authority `{authorities['real_composition']['artifact_content_hash']}` |",
        f"| Evaluation dataset | `{authorities['evaluation_dataset']['dataset_id']}` v`{authorities['evaluation_dataset']['dataset_version']}`; `{authorities['evaluation_dataset']['dataset_content_hash']}` |",
        f"| Evaluation | `{authorities['evaluation']['eval_run_id']}`; semantic `{authorities['evaluation']['evaluation_semantic_hash']}`; authority `{authorities['evaluation']['artifact_content_hash']}` |",
        "",
        "#### Typed evaluation results",
        "",
    ]
    for case in baseline["evaluation_cases"]:
        lines.extend(
            (
                f"Case `{case['case_id']}` for Run `{case['run_id']}` "
                f"(semantic `{case['case_semantic_hash']}`, authority "
                f"`{case['artifact_content_hash']}`):",
                "",
                "| Metric | Definition hash | Status | Type | Value |",
                "| --- | --- | --- | --- | --- |",
            )
        )
        for metric in case["metrics"]:
            if metric["value_type"] == "ratio":
                value = (
                    f"`{metric['value']}` "
                    f"(`{metric['numerator']}/{metric['denominator']}`)"
                )
            elif "value" in metric:
                value = f"`{metric['value']}`"
            else:
                value = f"`{metric.get('reason_code', 'not available')}`"
            lines.append(
                f"| `{metric['metric_id']}` | `{metric['definition_hash']}` | "
                f"`{metric['status']}` | `{metric['value_type']}` | {value} |"
            )
        lines.append("")
    lines.extend(
        (
            "#### Artifact byte hashes",
            "",
            "| Path | Bytes | SHA-256 |",
            "| --- | ---: | --- |",
        )
    )
    for artifact in baseline["artifacts"]:
        lines.append(
            f"| `{artifact['path']}` | `{artifact['byte_count']}` | "
            f"`{artifact['sha256']}` |"
        )
    lines.extend(("", "<!-- END schema-v1 semantic baseline -->"))
    return "\n".join(lines)


def _write_measurement(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_value_bytes(value) + b"\n"
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temp = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure the frozen schema-v1 semantic baseline offline."
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--print-markdown", action="store_true")
    args = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    expected_corpus = (
        repository_root / "tests" / "fixtures" / "schema_v1"
    ).resolve()
    supplied_corpus = args.corpus
    if supplied_corpus.is_symlink():
        parser.error("--corpus must not be a symbolic link")
    corpus = supplied_corpus.resolve()
    if corpus != expected_corpus:
        parser.error("--corpus must be the repository tests/fixtures/schema_v1 corpus")
    output = args.output.resolve()
    if output.is_relative_to(corpus):
        parser.error("--output must not be inside the frozen fixture corpus")
    started_at = datetime.now(UTC)
    started = time.perf_counter_ns()
    try:
        baseline = collect_semantic_baseline(corpus)
    except Exception as exc:
        print(
            f"baseline measurement failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    completed_at = datetime.now(UTC)
    occurrence = {
        **baseline,
        "occurrence": {
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
            "duration_ms": (time.perf_counter_ns() - started) // 1_000_000,
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "network_used": False,
            "credentials_read": False,
            "real_provider_calls": 0,
        },
    }
    _write_measurement(output, occurrence)
    if args.print_markdown:
        print(render_published_baseline(baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
