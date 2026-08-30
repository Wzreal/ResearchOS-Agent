"""Read-only canonical Phase 7 dataset loader."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from researchos.application.errors import (
    CorruptEvaluationDataset,
    EvaluationDatasetCompatibilityError,
    EvaluationDatasetNotFound,
    UnsafePersistenceData,
)
from researchos.domain.contracts import canonical_json_bytes
from researchos.domain.evaluation import EvaluationDataset
from researchos.security.redaction import PersistenceRedactor


class FilesystemEvaluationDatasetLoader:
    def __init__(self, root: str | Path, *, max_dataset_bytes: int) -> None:
        self._root = Path(root).resolve()
        self._max_dataset_bytes = max_dataset_bytes
        self._redactor = PersistenceRedactor()

    def load(
        self,
        dataset_id: str,
        dataset_version: str,
        *,
        expected_hash: str | None = None,
    ) -> EvaluationDataset:
        path = self._path(dataset_id, dataset_version)
        if not path.exists():
            raise EvaluationDatasetNotFound(f"dataset {dataset_id}@{dataset_version}")
        try:
            data = path.read_bytes()
            if len(data) > self._max_dataset_bytes:
                raise ValueError("dataset byte bound exceeded")
            dataset = EvaluationDataset.model_validate_json(data)
            self._redactor.assert_safe_model(dataset)
            canonical = canonical_json_bytes(dataset) + b"\n"
            if data != canonical:
                raise ValueError("dataset is not canonical JSON with one newline")
        except (OSError, ValueError, ValidationError, UnsafePersistenceData) as exc:
            raise CorruptEvaluationDataset("evaluation dataset is invalid") from exc
        if (
            dataset.dataset_id != dataset_id
            or dataset.dataset_version != dataset_version
            or (
                expected_hash is not None
                and dataset.dataset_content_hash != expected_hash
            )
        ):
            raise EvaluationDatasetCompatibilityError(
                "evaluation dataset identity/version/hash differs"
            )
        return dataset

    def _path(self, dataset_id: str, dataset_version: str) -> Path:
        safe = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not dataset_id or any(item not in safe for item in dataset_id):
            raise EvaluationDatasetCompatibilityError("unsafe dataset ID")
        if not dataset_version or any(
            item
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for item in dataset_version
        ):
            raise EvaluationDatasetCompatibilityError("unsafe dataset version")
        path = (self._root / dataset_id / f"{dataset_version}.json").resolve()
        if not path.is_relative_to(self._root):
            raise EvaluationDatasetCompatibilityError("dataset path escapes root")
        return path


class InMemoryEvaluationDatasetLoader:
    def __init__(self, datasets: tuple[EvaluationDataset, ...]) -> None:
        self._datasets = {
            (item.dataset_id, item.dataset_version): item.model_copy(deep=True)
            for item in datasets
        }

    def load(
        self,
        dataset_id: str,
        dataset_version: str,
        *,
        expected_hash: str | None = None,
    ) -> EvaluationDataset:
        try:
            result = self._datasets[(dataset_id, dataset_version)].model_copy(deep=True)
        except KeyError as exc:
            raise EvaluationDatasetNotFound(
                f"dataset {dataset_id}@{dataset_version}"
            ) from exc
        if expected_hash is not None and result.dataset_content_hash != expected_hash:
            raise EvaluationDatasetCompatibilityError("dataset hash differs")
        return result
