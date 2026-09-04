"""Minimal Phase 9 operator CLI adapter."""

from __future__ import annotations

import argparse
import json

from researchos.application.doctor import RealDoctor
from researchos.configuration.environment import (
    EnvironmentSecretSource,
    load_real_integration_settings,
)
from researchos.configuration.observability import load_otlp_http_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="researchos")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--real", action="store_true", required=True)
    doctor.add_argument("--probe-paid", action="store_true")
    doctor.add_argument("--probe-writes", action="store_true")
    args = parser.parse_args(argv)
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
