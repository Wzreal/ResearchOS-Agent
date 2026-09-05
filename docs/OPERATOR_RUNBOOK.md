# ResearchOS Operator Runbook

## Phase 11 protected REAL benchmark

Do not use this in CI or ordinary development. Provide a validated Phase 11
bundle, exact expected commit, clean approved ref, explicit acknowledgement,
and explicit approved cost (USD 50 is a ceiling, not authorization):

```shell
researchos benchmark --phase11-bundle <bundle.json> \
  --expected-commit-sha <40-hex-sha> --approved-cost-microunits <value> \
  --acknowledge-real --case-id p11_citation_chain
```

This is a limited portfolio-v1 smoke, not a full 12-case benchmark claim. Do
not launch the remaining cases or either REAL ablation automatically. Record
the first case's terminal state, provider/tool calls, input/output tokens,
latency, measured or upper-bound cost, Evidence/citations, and verification
outcome before seeking separate approval for any later REAL execution. Record
unavailable cost/latency as unavailable. Use one review per output, then a
blinded second review for the stratified 50% subset and adjudicate.

This runbook is for the implemented Phase 9 and Phase 10 profiles. It does not authorize
manual edits to durable JSON, provider retries, or Run-state guesses.

## Install and offline release gates

Use Python 3.11+ and `uv`. No credential is needed for these commands.

```shell
uv sync
uv run pytest --basetemp=.pytest_tmp -p no:cacheprovider -m "not requires_real_extra and not requires_real_smoke"
uv sync --all-extras
uv run pytest --basetemp=.pytest_tmp -p no:cacheprovider -m "not requires_real_smoke"
uv run ruff check .
git diff --check
uv build
```

Install the resulting wheel into a clean virtual environment with normal
dependency resolution, then verify `import researchos` and `researchos --help`.
Do not use an editable install or source checkout to substitute for this gate.

## Deterministic offline semantic baseline

The checked-in `tests/fixtures/schema_v1` corpus is byte-frozen. Its
`.gitattributes` rule disables EOL conversion; do not reformat it or regenerate
it from pytest/CI. Generate an ignored local occurrence record with:

```shell
uv run python scripts/measure_release_baseline.py \
  --corpus tests/fixtures/schema_v1 \
  --output outputs/measurements/offline-release-baseline.json
```

The stable projection is published in [RESULTS.md](RESULTS.md). The output's
timestamp, duration, OS, and Python details are local occurrence metadata only,
not a performance baseline. A failure indicates missing, tampered, noncanonical,
or incompatible fixture bytes; preserve the corpus and investigate rather than
repairing it in place.

## REAL preflight and doctor

Keep secrets outside Git. Use `.env.example` only as a variable reference; the
application reads environment variables and does not load a `.env` file itself.
For REAL mode, configure the canonical DeepSeek endpoint exactly as
`https://api.deepseek.com`, all required model/policy variables, and the
credential slot `RESEARCHOS_DEEPSEEK_API_KEY` through the host secret manager.

Optional capabilities require their own frozen configuration before use:

- `web_search`: Tavily configuration and `RESEARCHOS_TAVILY_API_KEY`;
- `web_browser`: Browser extra and its frozen policy;
- `managed_retrieval`: Zilliz serving endpoint, collection ID, operator Free-plan
  authority ID, and `RESEARCHOS_ZILLIZ_TOKEN`. The endpoint alone never proves a
  Free plan;
- OTLP: a direct HTTPS traces endpoint and an optional secret-sourced auth
  header value.

Run the zero-cost local check:

```shell
researchos doctor --real
```

It validates configuration, secret presence, optional dependencies, and local
policy only. It performs no DNS, HTTP, paid call, or remote write. The current
`--probe-paid` and `--probe-writes` flags deliberately fail closed; do not treat
doctor as a provider availability probe.

## Protected REAL smoke

The only REAL smoke is `.github/workflows/real-smoke.yml`:

- it is manual `workflow_dispatch` only;
- it accepts only `refs/heads/main`, checks out the event SHA, and has
  `contents: read` permission;
- it uses the fixed `researchos-real-smoke` GitHub Environment and requires an
  explicit paid-call acknowledgement;
- it makes one bounded planning call and requires a `VALIDATED` DAG.

Before dispatch, repository operators must configure Environment reviewers and
deployment branch restrictions. Workflow source cannot prove those external
settings. Never run this workflow from a feature branch, PR, user-supplied ref,
or unprotected Environment. Record only safe provenance in the release ticket:
workflow URL, commit, timestamp, model bundle hash, result status, validated
usage/certainty if available, and any separately reconciled invoice reference.

## Artifacts, recovery, and diagnostics

Generated `outputs/` are ignored and must not be committed. The canonical local
trace and the persisted authorities are source of truth; OTLP is only a
best-effort mirror. Inspect recovery in the authority order and follow the
fail-closed rules in [PHASE8_RECOVERY_RUNBOOK.md](PHASE8_RECOVERY_RUNBOOK.md).
Phase 10 provides one explicit offline workflow bundle:

```shell
researchos run "question" --mode mock
researchos inspect <run_id>
researchos resume <run_id>
```

`run` uses `phase10_mock@1`, `WorkflowFactory`, and `WorkflowCoordinator`.
Planning freezes the profile budget at `PLANNING_STARTED`; the validated DAG and
execution policy flow through `WorkflowRuntimeHandoff`. Existing checkpoint,
Evidence, Claim Extraction, ClaimGraph, verification, and evaluation
authorities remain the only durable authorities. `resume` constructs a fresh
filesystem-backed factory, checks the persisted profile pin, and derives work
only from durable artifacts. A terminal Run returns its existing result without
duplicate effects. `inspect` performs no mkdir, repair, credential read,
provider call, or persistent write.

`researchos run --mode real` intentionally fails closed until an explicit Phase
10 REAL workflow profile/configuration is supplied; it never falls back to
MOCK. Phase 9 historical REAL composition is three-role: planning, agent, and
verification. Phase 10 REAL composition additionally requires
`claim_extraction`, but this runbook does not authorize Phase 10 REAL calls.

`structural_selfcheck_v1` deterministically reconstructs one structural-only
case from persisted input and `Phase10WorkflowProfileV1` with the existing
Phase 7 evaluation harness. It is not benchmark quality evaluation, factual
quality evaluation, human evaluation, or Phase 11 benchmark evidence. Never
edit `run_state.json`, checkpoints, traces, evidence, claims, extraction
operations, verification operations, or evaluation artifacts by hand.

For corruption, composition mismatch, an unknown dispatched provider outcome,
or a missing optional dependency: stop, preserve artifacts, redact any external
diagnostic before sharing it, and resolve the configuration or operator-side
cause. Do not infer success, release UNKNOWN consumption, or retry provider
work outside the existing runtime policy.

## Release checklist and cleanup

- Complete every offline gate above, including the wheel check.
- Regenerate the local schema-v1 baseline occurrence and verify it matches the
  published stable projection in `RESULTS.md`.
- Confirm no generated outputs, `.env` files, credentials, review artifacts,
  or local measurement JSON are staged.
- If a paid smoke is required, use the protected main-only workflow and record
  its result separately from deterministic offline evidence.
- Rotate credentials in the secret manager, not in durable artifacts. Rotate a
  credential slot ID only through a new compatible REAL composition.
- Remove local `outputs/` only after copying any required incident evidence to
  an approved secure location.
