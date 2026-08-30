"""Atomic authority-last filesystem persistence for Phase 7."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from researchos.adapters._atomic_file import fsync_parent
from researchos.application.errors import (
    CorruptEvaluationArtifact,
    EvaluationArtifactConflict,
    EvaluationPersistenceError,
    UnsafePersistenceData,
)
from researchos.application.evaluation_identity import (
    ablation_artifact_hash,
    comparison_artifact_hash,
    validate_evaluation_hashes,
)
from researchos.domain.contracts import canonical_json_bytes
from researchos.domain.evaluation import (
    AblationResult,
    EvaluationComparison,
    EvaluationRun,
)
from researchos.security.redaction import PersistenceRedactor


class FilesystemEvaluationArtifactStore:
    def __init__(
        self,
        root: str | Path,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._fault = fault or (lambda _: None)
        self._redactor = PersistenceRedactor()

    def load(self, eval_run_id: str) -> EvaluationRun | None:
        directory = self._evaluation_dir(eval_run_id)
        authority = directory / "evaluation.json"
        if not authority.exists():
            return None
        try:
            result = EvaluationRun.model_validate_json(authority.read_bytes())
            if result.eval_run_id != eval_run_id:
                raise ValueError("evaluation authority identity differs")
            self._redactor.assert_safe_model(result)
            validate_evaluation_hashes(result)
            for case in result.cases:
                case_path = self._case_path(directory, case)
                persisted = type(case).model_validate_json(case_path.read_bytes())
                if persisted != case:
                    raise ValueError("case artifact differs from authority")
        except (
            OSError,
            ValueError,
            ValidationError,
            UnsafePersistenceData,
        ) as exc:
            raise CorruptEvaluationArtifact("evaluation authority is corrupt") from exc
        return result

    def publish(self, result: EvaluationRun) -> None:
        self._redactor.assert_safe_model(result)
        validate_evaluation_hashes(result)
        current = self.load(result.eval_run_id)
        if current is not None:
            if current != result:
                raise EvaluationArtifactConflict(
                    "same evaluation identity has another authority"
                )
            return
        directory = self._evaluation_dir(result.eval_run_id)
        for case in sorted(result.cases, key=lambda item: item.case_id):
            self._write(
                self._case_path(directory, case),
                canonical_json_bytes(case) + b"\n",
                stage=f"case.{case.case_id}",
                artifact_id=result.eval_run_id,
                authority=False,
            )
        authority_bytes = canonical_json_bytes(result) + b"\n"
        self._write(
            directory / "evaluation.json",
            authority_bytes,
            stage="authority",
            artifact_id=result.eval_run_id,
            authority=True,
        )

    def load_comparison(self, comparison_id: str) -> EvaluationComparison | None:
        path = self._comparison_dir(comparison_id) / "comparison.json"
        if not path.exists():
            return None
        try:
            result = EvaluationComparison.model_validate_json(path.read_bytes())
            if result.comparison_id != comparison_id:
                raise ValueError("comparison identity differs")
            self._redactor.assert_safe_model(result)
            if result.artifact_content_hash != comparison_artifact_hash(result):
                raise ValueError("comparison artifact hash differs")
        except (OSError, ValueError, ValidationError, UnsafePersistenceData) as exc:
            raise CorruptEvaluationArtifact("comparison authority is corrupt") from exc
        return result

    def publish_comparison(self, comparison: EvaluationComparison) -> None:
        self._redactor.assert_safe_model(comparison)
        if comparison.artifact_content_hash != comparison_artifact_hash(comparison):
            raise CorruptEvaluationArtifact("comparison artifact hash differs")
        current = self.load_comparison(comparison.comparison_id)
        if current is not None:
            if current != comparison:
                raise EvaluationArtifactConflict("comparison identity conflict")
            return
        self._write(
            self._comparison_dir(comparison.comparison_id) / "comparison.json",
            canonical_json_bytes(comparison) + b"\n",
            stage="comparison",
            artifact_id=comparison.comparison_id,
            authority=True,
        )

    def load_ablation(self, ablation_id: str) -> AblationResult | None:
        path = self._ablation_dir(ablation_id) / "ablation.json"
        if not path.exists():
            return None
        try:
            result = AblationResult.model_validate_json(path.read_bytes())
            if result.ablation_id != ablation_id:
                raise ValueError("ablation identity differs")
            self._redactor.assert_safe_model(result)
            if result.artifact_content_hash != ablation_artifact_hash(result):
                raise ValueError("ablation artifact hash differs")
        except (OSError, ValueError, ValidationError, UnsafePersistenceData) as exc:
            raise CorruptEvaluationArtifact("ablation authority is corrupt") from exc
        return result

    def publish_ablation(self, ablation: AblationResult) -> None:
        self._redactor.assert_safe_model(ablation)
        if ablation.artifact_content_hash != ablation_artifact_hash(ablation):
            raise CorruptEvaluationArtifact("ablation artifact hash differs")
        self._write(
            self._ablation_dir(ablation.ablation_id) / "ablation.json",
            canonical_json_bytes(ablation) + b"\n",
            stage="ablation",
            artifact_id=ablation.ablation_id,
            authority=True,
        )

    def _write(
        self,
        path: Path,
        data: bytes,
        *,
        stage: str,
        artifact_id: str,
        authority: bool,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / f".{uuid4().hex[:16]}.tmp"
        created = False
        try:
            self._fault(f"{stage}.before_temp_write")
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._fault(f"{stage}.before_claim")
            try:
                os.link(temp, path)
                created = True
                fsync_parent(path.parent)
            except FileExistsError as exc:
                if path.read_bytes() != data:
                    raise EvaluationArtifactConflict(
                        "same artifact identity has different bytes"
                    ) from exc
            self._fault(f"{stage}.after_claim")
        except EvaluationArtifactConflict:
            raise
        except Exception as exc:
            raise EvaluationPersistenceError(
                "atomic evaluation artifact write failed",
                artifact_id=artifact_id,
                authority_replaced=authority and created,
            ) from exc
        finally:
            temp.unlink(missing_ok=True)

    def _evaluation_dir(self, identity: str) -> Path:
        self._safe_id(identity)
        result = (self._root / identity).resolve()
        if result.parent != self._root:
            raise EvaluationArtifactConflict("evaluation path escapes root")
        return result

    @staticmethod
    def _case_path(directory: Path, case) -> Path:
        return (
            directory
            / "cases"
            / f"{case.case_id}.{case.artifact_content_hash[:24]}.json"
        )

    def _comparison_dir(self, identity: str) -> Path:
        self._safe_id(identity)
        base = (self._root / "comparisons").resolve()
        result = (base / identity).resolve()
        if result.parent != base:
            raise EvaluationArtifactConflict("comparison path escapes root")
        return result

    def _ablation_dir(self, identity: str) -> Path:
        self._safe_id(identity)
        base = (self._root / "ablations").resolve()
        result = (base / identity).resolve()
        if result.parent != base:
            raise EvaluationArtifactConflict("ablation path escapes root")
        return result

    @staticmethod
    def _safe_id(value: str) -> None:
        if not value or any(
            item not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for item in value
        ):
            raise EvaluationArtifactConflict("unsafe evaluation identity")
