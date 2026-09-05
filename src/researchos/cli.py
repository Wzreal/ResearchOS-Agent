"""Operator CLI with a strictly read-only Phase 10 inspection path."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from researchos.adapters._snapshot_jsonl import decode_snapshot
from researchos.application.doctor import RealDoctor
from researchos.application.workflow_factory import WorkflowFactory
from researchos.configuration.environment import (
    EnvironmentSecretSource,
    load_real_integration_settings,
)
from researchos.configuration.observability import load_otlp_http_settings
from researchos.configuration.phase10_mock import build_phase10_mock_bundle_v1
from researchos.domain.claim_extraction_operation import (
    ClaimExtractionOperationEnvelope,
)
from researchos.domain.contracts import (
    BudgetLimits,
    OperatingMode,
    RunConfig,
    RunInput,
    RunState,
)
from researchos.domain.evaluation import EvaluationRun
from researchos.domain.runtime import CheckpointEnvelope
from researchos.domain.synthesis import VerificationResult
from researchos.domain.workflow import WorkflowRuntimeHandoffEnvelope


def _read_json(path: Path, model: type[Any]) -> dict[str, object]:
    """Read one existing authority without directory creation or repair."""

    if not path.exists():
        return {"present": False}
    try:
        value = model.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return {"present": True, "status": "corrupt"}
    return {"present": True, "status": "valid", "value": value}


def _snapshot_projection(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"present": False}
    try:
        header, records = decode_snapshot(path.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {"present": True, "status": "corrupt"}
    return {
        "present": True,
        "status": "valid",
        "revision": header["store_revision"],
        "record_count": len(records),
    }


def _operations_projection(directory: Path) -> dict[str, object]:
    if not directory.exists():
        return {"present": False}
    items = sorted(directory.glob("*.json"))
    try:
        operations = [
            ClaimExtractionOperationEnvelope.model_validate_json(path.read_bytes())
            for path in items
        ]
    except (OSError, ValidationError, ValueError):
        return {"present": True, "status": "corrupt"}
    return {
        "present": True,
        "status": "valid",
        "count": len(operations),
        "statuses": [item.operation.status.value for item in operations],
    }


def inspect_run(root: str | Path, run_id: str) -> dict[str, object]:
    """Return a safe projection without constructing a mutable store."""

    base = Path(root).resolve()
    safe = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    if not run_id or any(char not in safe for char in run_id):
        raise ValueError("unsafe run ID")
    run_dir = (base / run_id).resolve()
    if run_dir.parent != base:
        raise ValueError("run path escapes output root")
    state_result = _read_json(run_dir / "run_state.json", RunState)
    state = state_result.pop("value", None)
    if state is None:
        return {"run_id": run_id, "run": state_result}
    assert isinstance(state, RunState)
    handoff = _read_json(
        run_dir / "workflow_runtime_handoff.json", WorkflowRuntimeHandoffEnvelope
    )
    checkpoint = _read_json(run_dir / "checkpoint.json", CheckpointEnvelope)
    handoff_value = handoff.pop("value", None)
    checkpoint_value = checkpoint.pop("value", None)
    verification = _read_json(run_dir / "verification.json", VerificationResult)
    verification.pop("value", None)
    evaluations = sorted((base / "evaluations").glob("*/evaluation.json"))
    evaluation_results = []
    for path in evaluations:
        result = _read_json(path, EvaluationRun)
        value = result.get("value")
        if isinstance(value, EvaluationRun) and any(
            case.run_id == state.run_id for case in value.cases
        ):
            evaluation_results.append(result)
    for result in evaluation_results:
        result.pop("value", None)
    return {
        "run_id": state.run_id,
        "revision": state.revision,
        "status": state.status.value,
        "input_redacted": state.input_redacted,
        "workflow_handoff": (
            {**handoff, "status": handoff_value.handoff.status.value}
            if handoff_value is not None
            else handoff
        ),
        "checkpoint": (
            {
                **checkpoint,
                "status": checkpoint_value.checkpoint.executor_status.value,
                "revision": checkpoint_value.checkpoint.checkpoint_revision,
                "task_counts": {
                    status: sum(
                        task.status.value == status
                        for task in checkpoint_value.checkpoint.task_states
                    )
                    for status in sorted(
                        {
                            task.status.value
                            for task in checkpoint_value.checkpoint.task_states
                        }
                    )
                },
            }
            if checkpoint_value is not None
            else checkpoint
        ),
        "evidence": _snapshot_projection(run_dir / "evidence.jsonl"),
        "claim_extraction_operations": _operations_projection(
            run_dir / "claim_extraction_operations"
        ),
        "claim_graph": _snapshot_projection(run_dir / "claims.jsonl"),
        "verification": verification,
        "evaluation": (
            {"present": False}
            if not evaluation_results
            else {"present": True, "runs": evaluation_results}
        ),
    }


def _mock_config() -> RunConfig:
    allocation = build_phase10_mock_bundle_v1().workflow_profile.budget.allocation
    return RunConfig(
        mode=OperatingMode.MOCK,
        budget_limits=BudgetLimits(
            max_duration_seconds=allocation.total.duration_milliseconds // 1_000,
            max_tokens=allocation.total.tokens,
            max_cost_microunits=allocation.total.cost_microunits,
            max_tool_calls=allocation.total.tool_calls,
        ),
        allowed_capability_ids=("read", "search"),
    )


def _mock_result(state: RunState) -> dict[str, str]:
    bundle = build_phase10_mock_bundle_v1()
    return {
        "run_id": state.run_id,
        "status": state.status.value,
        "mock_bundle_id": bundle.bundle_id,
        "mock_bundle_version": bundle.bundle_version,
        "workflow_profile_hash": bundle.workflow_profile.profile_hash,
    }


def _run_mock(root: str | Path, query: str) -> dict[str, str]:
    bundle = build_phase10_mock_bundle_v1()
    coordinator = WorkflowFactory(root).build_mock(bundle=bundle, query=query)
    state = asyncio.run(
        coordinator.create_and_execute(RunInput(query=query), _mock_config())
    )
    return _mock_result(state)


def _resume_mock(root: str | Path, run_id: str) -> dict[str, str]:
    base = Path(root).resolve()
    state_result = _read_json(base / run_id / "run_state.json", RunState)
    state = state_result.get("value")
    if not isinstance(state, RunState):
        raise ValueError("persisted Run is missing or corrupt")
    if state.config.mode is not OperatingMode.MOCK or state.input_redacted:
        raise ValueError("persisted Run is not resumable with phase10_mock@1")
    bundle = build_phase10_mock_bundle_v1()
    handoff_result = _read_json(
        base / run_id / "workflow_runtime_handoff.json", WorkflowRuntimeHandoffEnvelope
    )
    if handoff_result.get("status") == "corrupt":
        raise ValueError("persisted workflow profile differs from phase10_mock@1")
    handoff = handoff_result.get("value")
    if (
        handoff is not None
        and (
            handoff.handoff.workflow_profile_hash
            != bundle.workflow_profile.profile_hash
            or handoff.handoff.mock_bundle_id != bundle.bundle_id
            or handoff.handoff.mock_bundle_version != bundle.bundle_version
            or handoff.handoff.mock_bundle_hash != bundle.bundle_hash
        )
    ):
        raise ValueError("persisted workflow profile differs from phase10_mock@1")
    coordinator = WorkflowFactory(base).build_mock(
        bundle=bundle, query=state.input_snapshot.query
    )
    resumed = asyncio.run(
        coordinator.resume_and_execute(
            run_id,
            expected_input=RunInput(query=state.input_snapshot.query),
            expected_config=_mock_config(),
        )
    )
    return _mock_result(resumed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="researchos")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--real", action="store_true", required=True)
    doctor.add_argument("--probe-paid", action="store_true")
    doctor.add_argument("--probe-writes", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("question")
    run.add_argument("--mode", choices=("mock", "real"), required=True)
    run.add_argument("--outputs", default="outputs")
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--outputs", default="outputs")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("run_id")
    inspect.add_argument("--outputs", default="outputs")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        print(json.dumps(inspect_run(args.outputs, args.run_id), sort_keys=True))
        return 0
    if args.command == "run":
        if args.mode == "real":
            raise SystemExit("Phase 10 REAL workflow profile/config is required")
        print(json.dumps(_run_mock(args.outputs, args.question), sort_keys=True))
        return 0
    if args.command == "resume":
        print(json.dumps(_resume_mock(args.outputs, args.run_id), sort_keys=True))
        return 0
    if args.command in {"run", "resume"}:
        raise SystemExit(
            f"{args.command} requires an explicit Phase 10 fixture/profile; "
            "no implicit MOCK workflow is available"
        )
    settings = load_real_integration_settings()
    report = RealDoctor(
        settings=settings,
        secrets=EnvironmentSecretSource(),
        observability=load_otlp_http_settings(),
    ).run(probe_paid=args.probe_paid, probe_writes=args.probe_writes)
    print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    return 0 if all(item.status.value != "fail" for item in report.checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
