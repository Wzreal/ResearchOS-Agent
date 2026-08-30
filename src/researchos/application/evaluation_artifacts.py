"""One-read, bounded freezing of existing Phase 2-6 run artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from researchos.adapters._snapshot_jsonl import decode_snapshot
from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
from researchos.application.errors import (
    CorruptEvaluationInput,
    EvaluationInputChanged,
    EvaluationPreconditionError,
    UnsafePersistenceData,
)
from researchos.application.verification_publisher import render_markdown
from researchos.domain.claims import (
    CLAIM_SCHEMA_VERSION,
    ClaimEvidenceEdge,
    ClaimEvidenceEdgeRevision,
    ClaimGraphSnapshot,
    ClaimMutationReceipt,
    ClaimRecord,
    ClaimRevision,
)
from researchos.domain.contracts import (
    SCHEMA_VERSION,
    TERMINAL_STATUSES,
    RunState,
    TraceEvent,
    model_sha256,
)
from researchos.domain.evaluation import (
    ArtifactAvailability,
    ArtifactKind,
    ArtifactSnapshotRef,
    RunArtifactCompleteness,
    RunArtifactManifest,
)
from researchos.domain.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceRecord,
    EvidenceRevision,
    EvidenceStoreSnapshot,
    IngestionReceipt,
    SourceRecord,
)
from researchos.domain.identity import stable_hash
from researchos.domain.runtime import (
    RUNTIME_SCHEMA_VERSION,
    CheckpointEnvelope,
    RuntimeCheckpoint,
)
from researchos.domain.synthesis import VerificationResult
from researchos.security.redaction import PersistenceRedactor


@dataclass(frozen=True)
class FrozenRunArtifacts:
    manifest: RunArtifactManifest
    run_state: RunState | None
    checkpoint: RuntimeCheckpoint | None
    trace: tuple[TraceEvent, ...] | None
    evidence: EvidenceStoreSnapshot | None
    claims: ClaimGraphSnapshot | None
    verification: VerificationResult | None
    report_bytes: bytes | None
    raw_fingerprint: str = ""

    @property
    def corrupt(self) -> bool:
        return any(
            item.availability is ArtifactAvailability.CORRUPT
            for item in self.manifest.artifacts
        )


class ReadOnlyRunArtifactReader:
    _FILES = {
        ArtifactKind.RUN_STATE: "run_state.json",
        ArtifactKind.CHECKPOINT: "checkpoint.json",
        ArtifactKind.TRACE: "trace.jsonl",
        ArtifactKind.EVIDENCE: "evidence.jsonl",
        ArtifactKind.CLAIMS: "claims.jsonl",
        ArtifactKind.VERIFICATION: "verification.json",
        ArtifactKind.REPORT: "report.md",
    }

    def __init__(self, root: str | Path, *, max_input_bytes_per_case: int) -> None:
        self._root = Path(root).resolve()
        if not self._root.is_dir():
            raise CorruptEvaluationInput("run artifact root does not exist")
        if max_input_bytes_per_case < 1:
            raise ValueError("reader safety bound must be positive")
        self._absolute_max_bytes = max_input_bytes_per_case
        self._redactor = PersistenceRedactor()

    def freeze(
        self, run_id: str, *, max_input_bytes: int | None = None
    ) -> FrozenRunArtifacts:
        refs, raw = self._read_raw_snapshot(
            run_id, bound=self._effective_bound(max_input_bytes)
        )
        parsed: dict[ArtifactKind, Any] = {}
        final_refs: list[ArtifactSnapshotRef] = []
        for ref in refs:
            if ref.availability is ArtifactAvailability.ABSENT:
                final_refs.append(ref)
                continue
            try:
                value, schema_version = self._decode(
                    ref.artifact_kind, raw[ref.artifact_kind], run_id
                )
                parsed[ref.artifact_kind] = value
                final_refs.append(
                    ref.model_copy(update={"schema_version": schema_version})
                )
            except (
                CorruptEvaluationInput,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                ValidationError,
                UnsafePersistenceData,
            ):
                final_refs.append(
                    ref.model_copy(
                        update={
                            "availability": ArtifactAvailability.CORRUPT,
                            "corruption_reason_code": (
                                f"invalid_{ref.artifact_kind.value}"
                            ),
                        }
                    )
                )

        verification = parsed.get(ArtifactKind.VERIFICATION)
        report = parsed.get(ArtifactKind.REPORT)
        if verification is not None and report is not None and (
            hashlib.sha256(report).hexdigest() != verification.markdown_sha256
        ):
            final_refs = [
                item.model_copy(
                    update={
                        "availability": ArtifactAvailability.CORRUPT,
                        "corruption_reason_code": "report_hash_mismatch",
                    }
                )
                if item.artifact_kind is ArtifactKind.REPORT
                else item
                for item in final_refs
            ]
            parsed.pop(ArtifactKind.REPORT, None)

        state = parsed.get(ArtifactKind.RUN_STATE)
        has_corruption = any(
            item.availability is ArtifactAvailability.CORRUPT
            for item in final_refs
        )
        completeness = (
            RunArtifactCompleteness.CORRUPT
            if has_corruption
            else RunArtifactCompleteness.TERMINAL
            if state is not None and state.status in TERMINAL_STATUSES
            else RunArtifactCompleteness.NONTERMINAL_SNAPSHOT
        )
        values = dict(
            run_id=run_id,
            run_revision=None if state is None else state.revision,
            run_status=None if state is None else state.status.value,
            completeness=completeness,
            artifacts=tuple(final_refs),
        )
        provisional = RunArtifactManifest.model_construct(
            **values, manifest_hash="0" * 64
        )
        manifest = RunArtifactManifest(
            **values,
            manifest_hash=stable_hash(
                provisional.model_dump(mode="json", exclude={"manifest_hash"})
            ),
        )
        return FrozenRunArtifacts(
            manifest=manifest,
            raw_fingerprint=self._raw_fingerprint(refs),
            run_state=state,
            checkpoint=parsed.get(ArtifactKind.CHECKPOINT),
            trace=parsed.get(ArtifactKind.TRACE),
            evidence=parsed.get(ArtifactKind.EVIDENCE),
            claims=parsed.get(ArtifactKind.CLAIMS),
            verification=verification,
            report_bytes=parsed.get(ArtifactKind.REPORT),
        )

    def fingerprint(self, run_id: str, *, max_input_bytes: int | None = None) -> str:
        refs, _ = self._read_raw_snapshot(
            run_id, bound=self._effective_bound(max_input_bytes)
        )
        return self._raw_fingerprint(refs)

    def _effective_bound(self, requested: int | None) -> int:
        if requested is None:
            return self._absolute_max_bytes
        if requested < 1:
            raise EvaluationPreconditionError("case input byte bound is invalid")
        return min(self._absolute_max_bytes, requested)

    def _read_raw_snapshot(
        self, run_id: str, *, bound: int
    ) -> tuple[tuple[ArtifactSnapshotRef, ...], dict[ArtifactKind, bytes]]:
        if not run_id or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in run_id
        ):
            raise CorruptEvaluationInput("unsafe run identity")
        run_dir = self._root / run_id
        if run_dir.is_symlink():
            raise CorruptEvaluationInput("run directory cannot be a symlink")
        if run_dir.resolve().parent != self._root:
            raise CorruptEvaluationInput("run path escapes artifact root")
        refs: list[ArtifactSnapshotRef] = []
        raw: dict[ArtifactKind, bytes] = {}
        total = 0
        for kind in sorted(self._FILES, key=lambda item: item.value):
            path = run_dir / self._FILES[kind]
            if path.is_symlink():
                raise CorruptEvaluationInput("artifact path is a symlink")
            try:
                before = path.lstat()
            except FileNotFoundError:
                refs.append(
                    ArtifactSnapshotRef(
                        artifact_kind=kind,
                        relative_path=f"{run_id}/{self._FILES[kind]}",
                        availability=ArtifactAvailability.ABSENT,
                    )
                )
                continue
            except OSError as exc:
                raise CorruptEvaluationInput("artifact stat failed") from exc
            if not stat.S_ISREG(before.st_mode):
                raise CorruptEvaluationInput("artifact is not a regular file")
            if total + before.st_size > bound:
                raise EvaluationPreconditionError(
                    "case input artifact byte bound exceeded"
                )
            remaining = bound - total
            try:
                with path.open("rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if (
                        opened.st_dev != before.st_dev
                        or opened.st_ino != before.st_ino
                    ):
                        raise EvaluationInputChanged(
                            "artifact changed between stat and open"
                        )
                    data = handle.read(remaining + 1)
            except EvaluationInputChanged:
                raise
            except OSError as exc:
                raise CorruptEvaluationInput("run artifact cannot be read") from exc
            if len(data) > remaining:
                raise EvaluationPreconditionError(
                    "case input artifact byte bound exceeded"
                )
            if len(data) != opened.st_size:
                raise EvaluationInputChanged("artifact changed while being read")
            total += len(data)
            raw[kind] = data
            refs.append(
                ArtifactSnapshotRef(
                    artifact_kind=kind,
                    relative_path=f"{run_id}/{self._FILES[kind]}",
                    availability=ArtifactAvailability.PRESENT,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                )
            )
        return tuple(refs), raw

    @staticmethod
    def _raw_fingerprint(refs: tuple[ArtifactSnapshotRef, ...]) -> str:
        return stable_hash(
            [
                [
                    item.artifact_kind.value,
                    item.availability.value,
                    item.sha256,
                    item.size_bytes,
                ]
                for item in refs
            ]
        )

    def _decode(
        self, kind: ArtifactKind, data: bytes, run_id: str
    ) -> tuple[Any, int | None]:
        if kind is ArtifactKind.RUN_STATE:
            return self._decode_run_state(data, run_id), SCHEMA_VERSION
        if kind is ArtifactKind.CHECKPOINT:
            return self._decode_checkpoint(data, run_id), RUNTIME_SCHEMA_VERSION
        if kind is ArtifactKind.TRACE:
            return self._decode_trace(data, run_id), SCHEMA_VERSION
        if kind is ArtifactKind.EVIDENCE:
            return self._decode_evidence(data, run_id), EVIDENCE_SCHEMA_VERSION
        if kind is ArtifactKind.CLAIMS:
            return self._decode_claims(data, run_id), CLAIM_SCHEMA_VERSION
        if kind is ArtifactKind.VERIFICATION:
            result = VerificationResult.model_validate_json(data)
            self._redactor.assert_safe_model(result)
            if result.run_id != run_id:
                raise CorruptEvaluationInput("verification run identity differs")
            expected = render_markdown(
                result.final_draft,
                result.judge_decisions,
                findings=result.findings,
                resolved_finding_ids=result.resolved_finding_ids,
                citation_issues=result.citation_issues,
                disposition=result.disposition,
                acquisition_requests=result.acquisition_requests,
                omitted_claim_ids=result.omitted_claim_ids,
            )
            if hashlib.sha256(expected).hexdigest() != result.markdown_sha256:
                raise CorruptEvaluationInput("verification Markdown hash differs")
            return result, getattr(result, "schema_version", None)
        if kind is ArtifactKind.REPORT:
            data.decode("utf-8")
            return data, None
        raise CorruptEvaluationInput("unknown artifact kind")

    def _decode_run_state(self, data: bytes, run_id: str) -> RunState:
        raw = json.loads(data)
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise CorruptEvaluationInput("run state schema differs")
        state = RunState.model_validate(raw)
        self._redactor.assert_safe_model(state)
        if state.run_id != run_id:
            raise CorruptEvaluationInput("run state identity differs")
        if state.input_hash != model_sha256(state.input_snapshot):
            raise CorruptEvaluationInput("run input hash differs")
        if state.config_hash != model_sha256(state.config):
            raise CorruptEvaluationInput("run config hash differs")
        return state

    def _decode_checkpoint(self, data: bytes, run_id: str) -> RuntimeCheckpoint:
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise CorruptEvaluationInput("checkpoint envelope is not an object")
        checkpoint_raw = raw.get("checkpoint")
        if (
            not isinstance(checkpoint_raw, dict)
            or checkpoint_raw.get("schema_version") != RUNTIME_SCHEMA_VERSION
        ):
            raise CorruptEvaluationInput("checkpoint schema differs")
        checkpoint = CheckpointEnvelope.model_validate(raw).checkpoint
        self._redactor.assert_safe_model(checkpoint)
        if checkpoint.run_id != run_id:
            raise CorruptEvaluationInput("checkpoint run identity differs")
        if checkpoint.dag_hash != model_sha256(checkpoint.dag):
            raise CorruptEvaluationInput("checkpoint DAG hash differs")
        if checkpoint.execution_policy_hash != model_sha256(
            checkpoint.execution_policy
        ):
            raise CorruptEvaluationInput("checkpoint policy hash differs")
        return checkpoint

    def _decode_trace(self, data: bytes, run_id: str) -> tuple[TraceEvent, ...]:
        if data and not data.endswith(b"\n"):
            raise CorruptEvaluationInput("trace has a torn final event")
        events = []
        for line in data.splitlines():
            if not line:
                raise CorruptEvaluationInput("trace contains a blank line")
            raw = json.loads(line)
            if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
                raise CorruptEvaluationInput("trace schema differs")
            event = TraceEvent.model_validate(raw)
            self._redactor.assert_safe_model(event)
            if event.run_id != run_id:
                raise CorruptEvaluationInput("trace run identity differs")
            events.append(event)
        return tuple(events)

    def _decode_evidence(self, data: bytes, run_id: str) -> EvidenceStoreSnapshot:
        header, records = decode_snapshot(data)
        if (
            header.get("run_id") != run_id
            or header.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        ):
            raise CorruptEvaluationInput("evidence identity/schema differs")
        groups: dict[str, list[Any]] = {
            "source": [],
            "evidence": [],
            "revision": [],
            "receipt": [],
        }
        models = {
            "source": SourceRecord,
            "evidence": EvidenceRecord,
            "revision": EvidenceRevision,
            "receipt": IngestionReceipt,
        }
        for record_kind, payload in records:
            if record_kind not in models:
                raise CorruptEvaluationInput("unknown evidence record")
            groups[record_kind].append(models[record_kind].model_validate(payload))
        snapshot = EvidenceStoreSnapshot(
            run_id=run_id,
            store_revision=header["store_revision"],
            sources=tuple(groups["source"]),
            evidence=tuple(groups["evidence"]),
            revisions=tuple(groups["revision"]),
            receipts=tuple(groups["receipt"]),
        )
        self._redactor.assert_safe_model(snapshot)
        if FilesystemEvidenceStore._encode(snapshot) != data:
            raise CorruptEvaluationInput("evidence bytes are not canonical")
        return snapshot

    def _decode_claims(self, data: bytes, run_id: str) -> ClaimGraphSnapshot:
        header, records = decode_snapshot(data)
        if (
            header.get("run_id") != run_id
            or header.get("schema_version") != CLAIM_SCHEMA_VERSION
        ):
            raise CorruptEvaluationInput("claim identity/schema differs")
        groups: dict[str, list[Any]] = {
            "claim": [],
            "claim_revision": [],
            "edge": [],
            "edge_revision": [],
            "receipt": [],
        }
        models = {
            "claim": ClaimRecord,
            "claim_revision": ClaimRevision,
            "edge": ClaimEvidenceEdge,
            "edge_revision": ClaimEvidenceEdgeRevision,
            "receipt": ClaimMutationReceipt,
        }
        for record_kind, payload in records:
            if record_kind not in models:
                raise CorruptEvaluationInput("unknown claim record")
            groups[record_kind].append(models[record_kind].model_validate(payload))
        snapshot = ClaimGraphSnapshot(
            run_id=run_id,
            store_revision=header["store_revision"],
            claims=tuple(groups["claim"]),
            claim_revisions=tuple(groups["claim_revision"]),
            edges=tuple(groups["edge"]),
            edge_revisions=tuple(groups["edge_revision"]),
            receipts=tuple(groups["receipt"]),
        )
        self._redactor.assert_safe_model(snapshot)
        if FilesystemClaimGraphStore._encode(snapshot) != data:
            raise CorruptEvaluationInput("claim bytes are not canonical")
        return snapshot
