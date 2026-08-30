from __future__ import annotations

import pytest
from pydantic import ValidationError

from researchos.adapters.evaluation_dataset import (
    FilesystemEvaluationDatasetLoader,
)
from researchos.application.errors import (
    CorruptEvaluationDataset,
    EvaluationDatasetNotFound,
)
from researchos.domain.contracts import canonical_json_bytes
from researchos.domain.evaluation import (
    DatasetProvenance,
    EvaluationCase,
    EvaluationDataset,
    ReferenceAnnotations,
    ReferenceLevel,
)
from researchos.domain.identity import stable_hash


def _dataset(*cases, version="1"):
    values = dict(
        dataset_id="dataset_a",
        dataset_version=version,
        provenance=DatasetProvenance(
            license_id="test",
            source_uri_hash="0" * 64,
            curator_id="tests",
            provenance_version="1",
        ),
        cases=cases,
    )
    provisional = EvaluationDataset.model_construct(
        **values, dataset_content_hash="0" * 64
    )
    return EvaluationDataset(
        **values,
        dataset_content_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"dataset_content_hash"})
        ),
    )


def _case(case_id="case_a"):
    return EvaluationCase(
        case_id=case_id,
        reference_level=ReferenceLevel.STRUCTURAL_ONLY,
        query="query",
    )


def test_dataset_rejects_duplicate_and_noncanonical_case_ids():
    with pytest.raises(ValidationError, match="case IDs"):
        _dataset(_case(), _case())
    with pytest.raises(ValidationError, match="sorted"):
        _dataset(_case("case_b"), _case("case_a"))


def test_full_reference_requires_every_reference_block():
    with pytest.raises(ValidationError, match="every reference block"):
        EvaluationCase(
            case_id="case_full",
            reference_level=ReferenceLevel.FULL_REFERENCE,
            query="query",
            reference_annotations=ReferenceAnnotations(
                expected_capability_ids=("search",)
            ),
        )


def test_filesystem_dataset_is_versioned_canonical_and_bounded(tmp_path):
    dataset = _dataset(_case())
    path = tmp_path / dataset.dataset_id / "1.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(dataset) + b"\n")
    loader = FilesystemEvaluationDatasetLoader(tmp_path, max_dataset_bytes=100_000)
    assert (
        loader.load(
            dataset.dataset_id,
            dataset.dataset_version,
            expected_hash=dataset.dataset_content_hash,
        )
        == dataset
    )
    with pytest.raises(EvaluationDatasetNotFound):
        loader.load(dataset.dataset_id, "2")

    path.write_bytes(canonical_json_bytes(dataset))
    with pytest.raises(CorruptEvaluationDataset, match="invalid"):
        loader.load(dataset.dataset_id, dataset.dataset_version)

    path.write_bytes(canonical_json_bytes(dataset) + b"\n")
    bounded = FilesystemEvaluationDatasetLoader(tmp_path, max_dataset_bytes=1)
    with pytest.raises(CorruptEvaluationDataset):
        bounded.load(dataset.dataset_id, dataset.dataset_version)
