# ResearchOS Agent

ResearchOS Agent is a recoverable, verifiable multi-agent system for
complex, long-running deep-research tasks.

The project has completed Phases 0-8 and Phase 9A-9D plus release gates. It includes durable planning
and DAG execution, Agent/Tool adapters, Evidence and Claim authorities, bounded
verification, evaluation, resilience hardening, and an immutable REAL provider
composition boundary with DeepSeek Planning/Agent/Verification adapters.
The final Phase 9 measured-results and operator-runbook publication remains pending.

## Technical direction

- Durable DAG execution with checkpoint/resume and bounded resources
- Claim-level evidence provenance and conflict-aware verification
- Reproducible retrieval, trajectory, and report evaluation
- Explicit mock and real modes with no silent fallback
- Structured traces for important runtime state changes

See [Project Specification](docs/PROJECT_SPEC.md),
[Architecture](docs/ARCHITECTURE.md), [Task Board](docs/TASKS.md),
[Architecture Decisions](docs/DECISIONS.md), and
[Evaluation Plan](docs/EVALUATION.md).

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)

## Setup and verification

```shell
uv sync
uv run pytest
uv run ruff check .
```

The normal test suite is offline and cannot make paid provider calls. The
separate `REAL smoke` GitHub workflow is manual-only, accepts only `main` or a
`release-*` tag, requires an explicit paid-call acknowledgement, and runs in
the protected `researchos-real-smoke` Environment. Environment reviewers and
deployment-branch protection remain repository operator prerequisites.

## Import

```python
import researchos

print(researchos.__version__)
```

Mock mode remains explicit and deterministic. REAL mode requires an injected
Phase 9A integration guard, immutable `real_composition.json`, process-local
adapter binding, and credentials supplied outside durable artifacts. The
default `RunManager` still rejects unconfigured REAL mode and never falls back
to MOCK.

## Repository layout

```text
docs/             Project design, task board, ADRs, and evaluation plan
src/researchos/   Python package
tests/            Offline automated tests
examples/         Runnable examples added with implemented phases
outputs/          Generated run artifacts (ignored by Git)
```

Configuration names are documented in `.env.example`. Do not commit secrets or
generated output artifacts.
