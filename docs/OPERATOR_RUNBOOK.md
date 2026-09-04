# ResearchOS Operator Runbook

This runbook is for the implemented Phase 9 profile. It does not authorize
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
There is no generic `researchos run` or `researchos resume` CLI. An embedding
application must use the existing RunManager and durable verification APIs;
never edit `run_state.json`, checkpoints, traces, evidence, claims, or
verification operations by hand.

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
