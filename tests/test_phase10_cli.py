from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from researchos.adapters.clock import SystemClock
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.application.run_manager import RunManager
from researchos.application.workflow_factory import WorkflowFactory
from researchos.cli import _mock_config, inspect_run, main
from researchos.configuration.phase10_mock import build_phase10_mock_bundle_v1
from researchos.domain.contracts import RunInput, RunStatus
from researchos.domain.workflow import WorkflowRuntimeHandoffEnvelope


def _tree_bytes(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _cli(*args: str) -> dict[str, object]:
    scripts_dir = Path(sys.executable).parent
    executable = scripts_dir / ("researchos.exe" if os.name == "nt" else "researchos")
    completed = subprocess.run(
        [str(executable), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_inspect_is_read_only_and_uses_no_mutable_store_constructor(
    tmp_path, lifecycle
) -> None:
    _, _, _, clock, run_input, config = lifecycle
    trace = FilesystemTraceSink(tmp_path)
    runs = RunManager(store=FilesystemRunStore(tmp_path), trace_sink=trace, clock=clock)
    state = runs.create(run_input, config)
    before = (tmp_path / state.run_id / "run_state.json").read_bytes()

    result = inspect_run(tmp_path, state.run_id)

    assert result["status"] == "created"
    assert (tmp_path / state.run_id / "run_state.json").read_bytes() == before
    assert not (tmp_path / state.run_id / "checkpoint.json").exists()


def test_inspect_command_returns_safe_json(tmp_path, lifecycle, capsys) -> None:
    _, _, _, clock, run_input, config = lifecycle
    trace = FilesystemTraceSink(tmp_path)
    runs = RunManager(store=FilesystemRunStore(tmp_path), trace_sink=trace, clock=clock)
    state = runs.create(run_input, config)

    assert main(["inspect", state.run_id, "--outputs", str(tmp_path)]) == 0

    assert json.loads(capsys.readouterr().out)["run_id"] == state.run_id


@pytest.mark.parametrize("planning", [False, True])
def test_phase10_mock_cli_resume_recovers_preplanning_run(
    tmp_path, capsys, planning: bool
) -> None:
    runs = RunManager(
        store=FilesystemRunStore(tmp_path),
        trace_sink=FilesystemTraceSink(tmp_path),
        clock=SystemClock(),
    )
    state = runs.create(RunInput(query="fresh recovery question"), _mock_config())
    if planning:
        state = runs.transition(state.run_id, RunStatus.PLANNING)

    assert main(["resume", state.run_id, "--outputs", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_phase10_mock_cli_run_inspect_and_terminal_resume_are_durable(
    tmp_path,
) -> None:
    question = "What evidence supports the fixed deterministic research question?"

    run_result = _cli("run", question, "--mode", "mock", "--outputs", str(tmp_path))
    assert run_result["status"] == "completed"
    assert run_result["mock_bundle_id"] == "phase10_mock"
    assert run_result["mock_bundle_version"] == "1"
    assert run_result["workflow_profile_hash"] == (
        build_phase10_mock_bundle_v1().workflow_profile.profile_hash
    )
    run_id = run_result["run_id"]
    before_inspect = _tree_bytes(tmp_path)

    inspected = _cli("inspect", run_id, "--outputs", str(tmp_path))
    assert inspected["status"] == "completed"
    assert inspected["checkpoint"]["status"] == "completed"
    assert _tree_bytes(tmp_path) == before_inspect

    before_resume = _tree_bytes(tmp_path)
    resumed = _cli("resume", run_id, "--outputs", str(tmp_path))
    assert resumed == run_result
    # Resume constructs a new filesystem-backed factory inside the CLI.  A
    # terminal Run therefore returns only durable state and adds no task,
    # evidence, extraction, graph, verification, or evaluation effects.
    assert _tree_bytes(tmp_path) == before_resume


def test_phase10_mock_cli_resume_fails_closed_for_profile_mismatch(
    tmp_path, capsys
) -> None:
    assert (
        main(["run", "fixed question", "--mode", "mock", "--outputs", str(tmp_path)])
        == 0
    )
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    handoff_path = tmp_path / run_id / "workflow_runtime_handoff.json"
    envelope = WorkflowRuntimeHandoffEnvelope.model_validate_json(
        handoff_path.read_bytes()
    )
    tampered = envelope.handoff.model_copy(update={"workflow_profile_hash": "f" * 64})
    # The envelope remains structurally valid only when all linked hashes are
    # recomputed, so corrupting the stored pin must fail before resume.
    handoff_path.write_text(
        json.dumps(
            {
                **envelope.model_dump(mode="json"),
                "handoff": tampered.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="profile differs"):
        main(["resume", run_id, "--outputs", str(tmp_path)])


def test_phase10_mock_factory_rejects_unknown_bundle_identity(tmp_path) -> None:
    unknown = build_phase10_mock_bundle_v1().model_construct(
        bundle_id="unknown_mock", bundle_version="1"
    )

    with pytest.raises(ValueError):
        WorkflowFactory(tmp_path).build_mock(bundle=unknown, query="fixed question")


def test_phase10_real_cli_fails_closed_before_provider_composition(tmp_path) -> None:
    with pytest.raises(SystemExit, match="REAL workflow profile/config is required"):
        main(["run", "fixed question", "--mode", "real", "--outputs", str(tmp_path)])
    assert _tree_bytes(tmp_path) == {}
