# ResearchOS Agent

ResearchOS Agent is a recoverable, verifiable multi-agent system for
complex, long-running deep-research tasks.

The project has completed Phases 0-10 and Phase 9A-9D plus release gates. It includes durable planning
and DAG execution, Agent/Tool adapters, Evidence and Claim authorities, bounded
verification, evaluation, resilience hardening, and an immutable REAL provider
composition boundary with DeepSeek Planning/Agent/Verification adapters.
Published deterministic baseline evidence, cost semantics, limitations, and
operator procedures are available in [Results](docs/RESULTS.md) and the
[Operator Runbook](docs/OPERATOR_RUNBOOK.md).

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
separate `REAL smoke` GitHub workflow is manual-only, accepts only `main`,
requires an explicit paid-call acknowledgement, and runs in
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

Phase 10 exposes an explicit offline workflow:

```shell
researchos run "question" --mode mock
researchos inspect <run_id>
researchos resume <run_id>
```

`run` delegates to the filesystem-backed `WorkflowFactory` and
`WorkflowCoordinator`, pinning `phase10_mock@1` and a
`Phase10WorkflowProfileV1`. Resume reconstructs a fresh factory from durable
authorities and fails closed on a profile mismatch; inspect is strictly
read-only. Terminal resume is idempotent, while nonterminal resume rebuilds a
fresh filesystem-backed factory and continues solely from persisted authorities.
`--mode real` remains fail-closed without explicit Phase 10 REAL
configuration and never falls back to MOCK. `structural_selfcheck_v1` is a
deterministic structural self-check, not benchmark, factual-quality, human, or
Phase 11 evaluation evidence.

Phase 11 adds an operator-only REAL benchmark path. It requires an explicit
versioned bundle, exact checked-out commit, clean tree, approved ref,
`--acknowledge-real`, and explicit `--approved-cost-microunits`; USD 50 is a
hard ceiling, not spending approval. The frozen 12-case benchmark infrastructure
is available for future release-quality evaluation, but portfolio-v1 permits
only an explicitly authorized, bounded REAL smoke; it never automatically
continues to the other cases or to REAL ablations. Ordinary tests never invoke
a provider.

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
