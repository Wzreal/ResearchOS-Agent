# ResearchOS Agent Engineering Guide

This repository contains **ResearchOS Agent**, a recoverable and verifiable
multi-agent deep-research system. These instructions apply to the entire
repository.

## Required reading

Before changing code or design, read:

1. `docs/PROJECT_SPEC.md`
2. `docs/ARCHITECTURE.md`
3. `docs/TASKS.md`
4. `docs/DECISIONS.md`
5. `docs/EVALUATION.md` when work affects evaluation or metrics

## Engineering workflow

1. Inspect the existing implementation and current Git diff before editing.
2. State the proposed design, affected files, data flow, and risks.
3. Keep changes scoped to the active task.
4. Do not add placeholders, hard-coded success paths, fabricated data, or
   empty implementations to make a feature appear complete.
5. Put external systems behind narrow interfaces and adapters. Every external
   adapter must have an offline mock suitable for `pytest`.
6. Mock mode and real mode are explicit. Never silently fall back from real
   mode to mock mode.
7. Record every important runtime state transition as a structured trace event.
8. Add tests for every new behavior. Prefer deterministic, offline tests.
9. Use simple, clear, testable designs; introduce abstractions only when they
   protect an actual boundary.
10. After implementation, run the relevant tests and lint checks, then update
    `docs/TASKS.md`.
11. Add or amend an ADR in `docs/DECISIONS.md` for decisions that materially
    affect architecture, persistence, public contracts, security, or operating
    modes.

## Completion report

Every completed task must report:

- what was implemented;
- what remains unimplemented;
- exact test and lint results;
- recommended next phase.

## Repository conventions

- Python 3.11 or newer
- `uv` for environment and dependency management
- Pydantic v2 for validated contracts
- `pytest` for tests
- `ruff` for linting and formatting policy
- source layout under `src/researchos`
- generated run artifacts under `outputs/` and never committed

Reference projects may inform architecture, but do not copy large source blocks
or transplant whole modules from them.
