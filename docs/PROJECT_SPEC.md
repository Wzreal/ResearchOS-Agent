# ResearchOS Agent Project Specification

**Project:** ResearchOS Agent — a recoverable, verifiable multi-agent deep
research system  
**Status:** Phase 0 design baseline  
**Runtime:** Python 3.11+

## 1. Project goals

ResearchOS Agent executes complex, long-running research requests as durable,
observable runs. It must:

- turn a user query into perspectives and a validated task DAG;
- schedule independent work concurrently while enforcing dependency, retry,
  timeout, failure-isolation, and budget policies;
- persist checkpoints so interrupted work can resume without repeating
  completed side effects;
- collect source-grounded evidence and connect evidence to atomic claims;
- detect contradictory or weak support and apply bounded verification and
  correction;
- evaluate retrieval, trajectories, and final reports with reproducible inputs;
- produce a report whose claims can be traced to evidence and runtime events.

The project optimizes for correctness, auditability, recovery, and explicit
failure over superficially fluent output.

## 2. Non-goals

- General-purpose autonomous computer control.
- Unbounded recursive self-improvement or open-ended agent loops.
- Training or fine-tuning foundation models.
- Building a browser engine, vector database, or observability platform.
- Guaranteeing that every web source is true; the system records provenance,
  assesses support and conflict, and exposes uncertainty.
- Hiding partial failure or substituting mock output in real mode.
- Implementing production business behavior during Phase 0.

## 3. Core user flow

1. A user submits a research query plus optional constraints: deadline, cost,
   allowed tools, source policy, and output format.
2. `RunManager` creates a durable run identity, snapshots configuration, and
   emits the initial trace event.
3. `PerspectivePlanner` decomposes the question into complementary research
   perspectives and builds a dependency-aware task DAG.
4. The DAG is validated before execution. Invalid graphs fail explicitly.
5. `AsyncDAGExecutor` schedules ready tasks within concurrency and budget
   limits. Agents use tool interfaces for browser, local retrieval, and Python.
6. Results and provenance enter `EvidenceMemory`; atomic claims are linked to
   supporting, contradicting, or contextual evidence.
7. `Synthesizer` drafts a report from the claim-evidence graph rather than raw
   tool transcripts.
8. Red, Blue, and Judge roles inspect coverage, conflicts, and unsupported
   claims under a bounded correction budget.
9. The evaluation harness scores configured dimensions and records metric
   inputs, versions, and limitations.
10. The run publishes a final or explicitly partial report plus manifest,
    evidence, trace, and evaluation artifacts.

## 4. Core technical highlights

### 4.1 Durable DAG Agent Runtime

The runtime will combine DAG planning, asynchronous scheduling, retry and
timeout policy, checkpoint/resume, task-level failure isolation, dynamic
replanning, and explicit time/token/cost/tool-call budgets. Checkpoints capture
validated state transitions; resumption must not infer success from the mere
presence of a file.

### 4.2 Claim-level Evidence Verification

Evidence is normalized with stable identifiers, provenance, retrieval time,
content hashes, and source metadata. Atomic claims link to evidence with typed
relations and entailment/confidence assessments. Conflict detection and bounded
Red/Blue/Judge review target weak or contradictory claims without allowing an
unbounded loop.

### 4.3 Agent Evaluation Harness

Evaluation covers retrieval quality, trajectory quality, and report quality.
It supports deterministic fixtures, dataset versioning, ablation comparisons,
and correlation with structured traces. Metrics are reported only when actually
computed; Phase 0 defines contracts and candidate metrics, not results.

## 5. Mock mode and real mode

| Property | Mock mode | Real mode |
|---|---|---|
| Purpose | Offline development and deterministic tests | Actual external integrations |
| Selection | Explicit configuration, for example `RESEARCHOS_MODE=mock` | Explicit `RESEARCHOS_MODE=real` |
| Credentials | Not required | Validated at startup for enabled adapters |
| Responses | Fixture-driven and reproducible | Returned by configured providers |
| Network | Not required | Allowed only through configured adapters |
| Missing provider | Clear configuration error | Clear configuration error |
| Fallback | Never changes mode implicitly | Never falls back to mock |

Both modes implement the same domain-facing interfaces. Trace events identify
the selected mode and adapter, but must redact secrets. A mock must emulate
contract semantics and failure cases; it must not be a hard-coded “success”
shortcut.

## 6. Output file specification

Each run owns one immutable-id directory:

```text
outputs/<run_id>/
├── manifest.json
├── report.md
├── run_state.json
├── checkpoint.json
├── trace.jsonl
├── evidence.jsonl
├── claims.jsonl
├── evaluation.json
└── artifacts/
```

- `run_id` is generated by the runtime and is safe as a directory name.
- `manifest.json` records schema versions, creation/completion times, mode,
  sanitized configuration, input hash, and artifact hashes.
- `run_state.json` is a recoverable state snapshot written atomically.
- `checkpoint.json` is the Phase 3 atomic DAG-runtime snapshot. It stores the
  validated DAG/policy hashes, attempts, outcomes, budget/replan state, and the
  immutable last-mutation trace outbox used for bounded reconciliation.
- `trace.jsonl` is append-only and contains structured, timestamped events.
- `evidence.jsonl` and `claims.jsonl` contain versioned normalized records.
- `report.md` cites claim/evidence identifiers using a documented convention.
- `evaluation.json` records metric definitions, inputs, versions, results, and
  skipped metrics with reasons. It never contains invented values.
- `artifacts/` stores tool outputs that are safe and necessary to retain.
- Partial runs use the same layout and declare `status` and missing artifacts in
  the manifest; they are never presented as complete.
- Secrets, raw authorization headers, and unnecessary personal data must never
  be written to outputs or traces.

Schema definitions will be versioned before these files become runtime output.

## 7. Development phases and acceptance criteria

### Phase 0 — Architecture and minimal package

- Required project documents and ADR baseline exist and agree on boundaries.
- The Python package imports without side effects.
- No business component is represented by a fake implementation.
- `uv sync`, offline tests, and `ruff check .` pass.

### Phase 1 — Domain contracts and run lifecycle

- Pydantic v2 contracts exist for run configuration, status, budgets, task
  records, artifacts, and trace events.
- `RunManager` creates, loads, transitions, and finalizes runs through a tested
  storage interface.
- Invalid transitions and real-mode misconfiguration fail explicitly.
- Filesystem storage and deterministic in-memory test storage pass recovery and
  atomic-write tests.

### Phase 2 — Planning and DAG model

- Planner and model-provider interfaces are separated from domain logic.
- DAG schema validates unique nodes, dependencies, cycles, tool policy, and
  budget feasibility.
- Mock planning fixtures cover valid and invalid decompositions.
- Planner output and validation events are traceable.

### Phase 3 — Durable async DAG runtime

- Ready nodes execute concurrently within configured limits.
- Retry, timeout, cancellation, failure isolation, and dependency propagation
  have deterministic tests.
- Checkpoint/resume avoids rerunning committed task outcomes.
- Dynamic replanning is bounded, validated, budgeted, and traceable.

### Phase 4 — Agent and tool adapters

- Agent/tool protocols support local retrieval, Python, browser, and search
  without coupling the runtime to vendors.
- Every enabled real adapter has configuration validation and an offline mock.
- Tool permissions, input/output schemas, timeout, and artifact handling are
  tested; no silent mode fallback exists.

### Phase 5 — Evidence memory and claim graph

- Evidence records preserve provenance, content hashes, and deduplication
  semantics.
- Claims have atomic text, typed evidence edges, and revision history.
- Query, conflict-candidate, and citation-integrity behaviors are tested against
  deterministic fixtures.

### Phase 6 — Synthesis and bounded verification

- Synthesis consumes claim/evidence contracts and flags unsupported statements.
- Red/Blue/Judge roles have distinct inputs and auditable decisions.
- Correction loops enforce iteration and budget bounds.
- Conflicts, abstentions, and unresolved claims remain visible in final output.

### Phase 7 — Evaluation harness

- Versioned datasets and evaluators cover retrieval, trajectory, and report
  dimensions.
- Evaluations are reproducible and distinguish missing/skipped from zero.
- Ablation runs share inputs and record configuration differences.
- No metric result is emitted without its evidence and computation metadata.

### Phase 8 — Observability and resilience hardening

- Trace schemas cover lifecycle, scheduling, tools, evidence, verification,
  budgets, checkpoints, and evaluation.
- Correlation identifiers connect report claims to evidence and task events.
- Redaction, load, crash-recovery, and fault-injection tests pass.
- Optional observability exporters fail visibly without breaking local trace
  durability.

### Phase 9 — Real integrations and production readiness

- Selected LLM, browser/search, retrieval, and observability adapters pass
  contract and controlled integration tests.
- Security review, migration strategy, operator runbook, and release checklist
  are complete.
- Representative end-to-end runs meet documented quality and reliability gates.
- Known limitations and costs are published without fabricated benchmarks.
