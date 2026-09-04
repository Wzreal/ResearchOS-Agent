"""Regression tests for the Phase 9 final publication boundary."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "schema_v1"
RESULTS = ROOT / "docs" / "RESULTS.md"


def _script_module():
    path = ROOT / "scripts" / "measure_release_baseline.py"
    spec = importlib.util.spec_from_file_location("release_baseline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _published_block(document: str) -> str:
    start = "<!-- BEGIN schema-v1 semantic baseline -->"
    end = "<!-- END schema-v1 semantic baseline -->"
    return document[document.index(start) : document.index(end) + len(end)]


def test_schema_v1_measurement_is_stable_and_published(tmp_path, monkeypatch):
    module = _script_module()
    monkeypatch.setenv("RESEARCHOS_MEASUREMENT_CANARY", "must-not-appear")
    first = module.collect_semantic_baseline(CORPUS)
    second = module.collect_semantic_baseline(CORPUS)

    assert first == second
    assert first["measurement_kind"] == (
        "deterministic_offline_schema_v1_semantic_baseline"
    )
    assert first["artifact_file_count"] == len(first["artifacts"])
    assert "must-not-appear" not in json.dumps(first, sort_keys=True)
    assert _published_block(RESULTS.read_text(encoding="utf-8")) == (
        module.render_published_baseline(first)
    )

    output = tmp_path / "baseline.json"
    assert (
        module.main(
            ["--corpus", str(CORPUS), "--output", str(output)]
        )
        == 0
    )
    persisted = json.loads(output.read_bytes())
    occurrence = persisted.pop("occurrence")
    assert persisted == first
    assert occurrence["network_used"] is False
    assert occurrence["credentials_read"] is False
    assert occurrence["real_provider_calls"] == 0


@pytest.mark.parametrize("replacement", [(b"\r\n", b"\n"), (b"x", b"y")])
def test_measurement_fails_closed_for_noncanonical_or_tampered_fixture(
    tmp_path, replacement
):
    module = _script_module()
    copied = tmp_path / "schema_v1"
    shutil.copytree(CORPUS, copied)
    target = copied / "run_1" / "evidence.jsonl"
    original = target.read_bytes()
    if replacement == (b"\r\n", b"\n"):
        target.write_bytes(original.replace(b"\n", b"\r\n"))
    else:
        target.write_bytes(original.replace(*replacement, 1))
    output = tmp_path / "must_not_publish.json"

    assert module.main(["--corpus", str(copied), "--output", str(output)]) == 1
    assert not output.exists()


def test_schema_fixture_bytes_are_eol_preserved_by_git_attributes():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "tests/fixtures/schema_v1/** -text" in attributes
    for path in CORPUS.rglob("*"):
        if path.is_file():
            assert b"\r\n" not in path.read_bytes(), path
