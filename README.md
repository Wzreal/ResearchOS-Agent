# ResearchOS Agent

ResearchOS Agent is a planned recoverable, verifiable multi-agent system for
complex, long-running deep-research tasks.

The project has completed **Phase 3**. It now includes provider-independent
planning plus a durable asynchronous DAG runtime with deterministic scheduling,
retry/timeout/cancellation policy, task failure isolation, atomic checkpoints,
crash reconciliation, two-level idempotency keys, additive budget accounting,
and durable bounded replan requests. Agents, tools, real providers, retrieval,
evidence memory, verification roles, and evaluation execution are not
implemented.

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

## Import

```python
import researchos

print(researchos.__version__)
```

The implemented phases support lifecycle, planning, and DAG runtime tests in
explicit mock mode. The Phase 3 execution backend is a narrow
capability-neutral port with an exact-fixture offline mock; it is not an Agent
or Tool implementation. Real mode still fails before persistence because Phase
4 real adapters do not exist, and it never falls back to mock.

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
