# Phase 9 Design — REAL integrations, observability, and release gates

## Scope

Phase 9A adds safe REAL LLM composition and DeepSeek adapters. Phase 9B adds
composition-frozen Tavily Search and a bounded HTTP Browser without changing
Phase 1-8 authority schemas. Phase 9C adds bounded Zilliz BM25 retrieval.
Playwright, embeddings, hybrid retrieval,
Langfuse, Claim Extraction, workflow coordination, real E2E, and benchmark
results and the final operator runbook are not implemented. Phase 9 now also
has release gates: an exact public DeepSeek origin allowlist, frozen v1
compatibility fixtures, deterministic offline filesystem E2E, wheel
installability CI, and a separately protected manual REAL smoke workflow.

## Phase 9 release-gate boundary

The supported DeepSeek REAL endpoint is exactly `https://api.deepseek.com`.
Custom gateways, paths, explicit ports, userinfo, query strings and alternate
hosts fail before a credential-bearing transport is constructed. This is a
release profile and does not claim to make arbitrary provider endpoints safe.

No general migration framework is introduced: the persisted contracts remain
schema v1. `tests/fixtures/schema_v1` is a checked-in, byte-frozen release
compatibility corpus. Tests load it through fresh adapters and never regenerate
it; unknown, missing, tampered, and torn data fail closed.

The deterministic filesystem E2E follows the legal RunManager lifecycle from
`CREATED` through planning, durable execution, verification, evaluation, and
`COMPLETED`. Evaluation is explicitly read-only and runs during `EVALUATING`
with its nonterminal policy opt-in.

The only REAL smoke workflow is `workflow_dispatch`, protected by GitHub
Environment `researchos-real-smoke`, a paid-call acknowledgement, and an
allowed-ref gate (`main` or `release-*`). The workflow cannot establish that
Environment reviewers or deployment branch rules have been configured; those
are mandatory operator release prerequisites.

## Phase 9D optional observability boundary

Phase 9D mirrors already-accepted canonical local trace events to optional
OTLP/HTTP protobuf export. `ObservabilityRuntime` owns bounded background
workers so synchronous trace callers never require an asyncio loop. Local
append remains first and authoritative; append-once no-ops are not exported,
observability diagnostics remain local-only, and remote delivery is
non-durable/no-retry best effort. OTLP permits direct HTTPS only, disables
redirects and environment proxies, bounds HTTP/request/response resources, and
uses only a validated secret-sourced auth header. Disabled mode creates no
exporter worker.

## Phase 9C retrieval boundary

Phase 9C adds only Zilliz Cloud Free-plan BM25 retrieval. It uses
the existing Local Retrieval request/result contracts, but queries one
operator-provisioned read-only collection through exactly one search RPC. The
endpoint must be canonical `https://<cluster>.serverless.<region>.vectordb.zillizcloud.com/`;
arbitrary Milvus, private, global, on-demand, Dedicated and explicit-port
endpoints fail before credential lookup. This hostname proves only the shared
Free-or-Serverless endpoint family, never a Free plan. A zero-cost reservation
requires an immutable operator-provisioned Free-plan attestation frozen into
the retrieval policy and REAL composition hash; missing or non-Free authority
fails closed. All billable/variable profiles are unsupported rather than
represented by an operator estimate.

The frozen collection schema includes document/chunk IDs, locator, content,
stored content SHA-256 and bounded safe metadata. The adapter recomputes the
returned UTF-8 content hash and rejects a mismatch before constructing a
LocalRetrievalResult or permitting Phase 5 Evidence ingestion. No schema
inspection RPC, collection mutation, embedding, dense/hybrid retrieval or
reranking is performed. A retrieval-enabled Agent receives a new v3 tool
response contract; Phase 9A direct v1 and Phase 9B web-only v2 remain frozen.

## Composition authority

`ModelCallPolicySnapshot` freezes generation controls, the post-response
input-token integrity cap, output-token cap, request/response byte bounds,
connect/read/write/pool timeouts,
currency, per-call cost ceiling, and versioned per-model pricing upper-bound
rules. It also freezes the adapter-owned pricing safety profile and derived
provider-call token/cost reservation. Each role's `ModelBundlePayload`
also pins provider/profile/model, adapter, canonical base endpoint including
path, prompt content, response contract, and the safe credential-slot ID.
Credential values remain non-semantic and may rotate; changing the slot ID
changes the bundle and composition identities.

The non-circular identity order is:

```text
policy preimage -> model_call_policy_hash
bundle preimage -> model_bundle_hash
semantic composition preimage -> composition_hash
[run_id, run_config_hash, composition_hash] -> composition_id
```

`RealCompositionEnvelope` adds the first-write `recorded_at` and a complete
artifact content hash. Resume equality excludes occurrence metadata. Every
load reparses exact bytes and independently verifies every identity layer.

Filesystem first-writer creation shares a normalized absolute-path lock across
adapter instances. Reload, compare, and write are one critical section. Equal
semantic snapshots return the original envelope; different snapshots raise a
typed conflict. Cross-process CAS is not claimed.

## Run lifecycle boundary

Default `RunManager` behavior still rejects REAL mode. A configured deployment
injects `RealCompositionManager` as `RunIntegrationGuard`:

```text
create preflight -> persist CREATED -> create authority -> process-local bind
mutation load -> trace reconcile -> validate current authority -> mutate
resume reconcile -> validate/rebind -> append run.resumed
```

Every existing REAL mutation uses `_load_for_mutation()`. Entering PLANNING also
requires the local binding. Adapter construction requests a bound role from
`RealIntegrationFactory`; missing binding, secret, authority, or role fails
before dispatch. A read-only lifecycle authorizer also checks every adapter
construction and call: ordinary Planning requires `PLANNING`, the narrow
trusted runtime-replan path requires `RUNNING`, Agent requires `RUNNING`, and
Verification requires `VERIFYING`. Immediately before HTTP it also reloads and
validates `real_composition.json`, recomputes current semantics, and matches the
adapter's process-local composition binding. Deletion, corruption, or drift
therefore blocks an already-constructed adapter.

## DeepSeek provider boundary

`OpenAICompatibleChatTransport` has explicit sync and async one-call paths. It
uses non-streaming structured JSON, safe fixed headers, streaming raw-response
byte enforcement, strict model/usage validation, configured HTTPX timeouts,
and no retry. Provider exception messages are replaced by stable codes. Its
diagnostic contract distinguishes `NOT_DISPATCHED`,
`DISPATCHED_OUTCOME_UNKNOWN`, and `RESPONSE_RECEIVED`; this is transport
diagnostic data and never overrides Phase 8 journal authority.

The Phase 9A profile uses `https://api.deepseek.com`. Planning and Verification
use `deepseek-v4-pro`; Agent uses `deepseek-v4-flash`. All three roles explicitly
pin and send `thinking=enabled` and `reasoning_effort=high` by default. With
thinking enabled, `temperature` and `top_p` are canonically absent because the
provider ignores them. Disabled thinking makes both controls required and
effective.

DeepSeek endpoints are HTTPS-only. Parent cancellation cancels and joins both
the provider operation and cancellation waiter. Only `finish_reason=stop` is
successful. Length, content-filter, unexpected Tool-call, and
insufficient-resource completions are stable `RESPONSE_RECEIVED` failures;
validated usage is preserved and the adapter never retries. Successful
responses must fit the frozen input/output token caps. `max_input_tokens` is a
post-response integrity bound only. HTTP request bytes are only a transport
bound and are never converted to tokens. Before dispatch, the adapter-owned
model safety profile reserves a conservative provider/model input-token upper
bound plus the configured output cap. Provider-reported token counts remain the
measurement source, while local monetary calculation charges every prompt
token at the model-specific maximum input rate and is always
`UsageCertainty.UPPER_BOUND`; cache hit/miss fields cannot lower it. The derived
reservation must fit both the Run budget and, for Agent calls, the task hard
limit before HTTP. This is admission metadata consumed at existing budget
boundaries, not a second ledger. Planning and Verification rely on the frozen
Run compatibility preflight; Phase 3 continues to own runtime reservation and
settlement. Known HTTP status remains `RESPONSE_RECEIVED`, with sanitized
retryability metadata preserved for Phase 3 policy.

The safety profile is adapter-owned, versioned, model-specific, and supports
only `deepseek-v4-flash` and `deepseek-v4-pro` in Phase 9A. Its reviewed v2 data
pins the current 1,000,000-token context window, 384,000-token provider output
maximum, and USD billing currency. Without a proven local tokenizer, the full
context window is conservatively reserved as input; `max_input_tokens` remains
post-response integrity only. Configured output is additionally rejected above
the provider maximum. Operator rates may raise but cannot undercut conservative
minima, and non-USD configuration fails without FX conversion. These profiles
are reviewed safety data, not permanent provider price guarantees. The profile
version/hash, derived reservation, and pricing rules flow through policy,
bundle, and composition identity. DeepSeek-specific policy placement in the Phase 9 domain
is accepted for 9A and may later move behind a generic provider pin without
changing frozen semantics. Requested model aliases are what composition pins;
Phase 9A does not claim that an alias identifies immutable physical model
weights. `system_fingerprint`, if observed in a future response contract, is
safe provenance for later end-to-end/evaluation work and is not 9A recovery
authority.

Official-host allowlisting is deferred to Phase 9D security hardening. Phase 9A
continues to require HTTPS and pins the canonical endpoint identity without
claiming host allowlisting.

Phase 9B exposes only the typed Search/Browser Tool decision union to the REAL
Agent. Capability descriptors, policies, reservations, and operation versions
are frozen in the per-Run composition. `AgentRunner` remains the final
authorization and local hard-limit admission owner; a separate ephemeral
authorized envelope protects the REAL adapter boundary without changing the v1
Tool invocation persistence contract.

The tool-capable Agent contract is selected only when the immutable REAL
settings contain Search or Browser. Capability-empty Phase 9A Runs keep the
original direct-only prompt/schema and `agent-direct-decision-v1`; Web-enabled
Runs use `agent-tool-decision-v2`, producing distinct prompt, model-bundle, and
composition hashes.

For that web Agent profile only, the frozen model policy includes a total
provider-call timeout. The provider reservation uses the same value and the
async transport enforces it as a whole-operation deadline in addition to its
phase-specific HTTPX stall timeouts. Capability-empty Phase 9A direct profiles
retain their historical per-call token/cost preflight and deadline behavior.

Tavily is fixed to its STANDARD HTTPS endpoint/profile with explicit documented
request fields, no redirect/proxy/retry, and conservative versioned credit-cost
reservation. Its summaries are provider metadata in `SearchResultV2` and yield
zero Evidence. The Browser reauthorizes each redirect, double-resolves and pins
ResearchOS-policy-approved addresses from an exact special-use table (including
IPv4-mapped IPv6 and mixed-DNS rules), validates the connected peer and TLS hostname, rejects
compression and unsupported MIME/charset, bounds bytes/time, and delegates text
extraction to a sanitized bounded subprocess. See
`docs/PHASE9_SECURITY_REVIEW.md` and ADR-0026.

`DeepSeekPlanningModel`, `DeepSeekAgent`, and
`DeepSeekVerificationModel` map this transport into existing ports. Agent usage
comes from the provider envelope rather than model-authored decision JSON.
Verification exceptions propagate unchanged so the Phase 8 durable journal
retains DISPATCHED precedence and UNKNOWN outcome semantics.

## Dependency and operator boundary

Core imports do not import HTTPX or later provider packages. Optional extras
are explicit, and a missing LLM extra raises `MissingOptionalDependency`.
Core CI runs Phase 0-8 plus provider-independent Phase 9 tests after plain
`uv sync`; a second offline job installs all extras, including Browser
extraction tests.

`researchos doctor --real` performs local configuration, secret-presence, and
optional composition and Phase 9B capability policy/dependency checks. It makes
zero paid calls, remote writes, DNS queries, or HTTP requests. Paid/write flags
fail explicitly until later bounded probes exist; provider availability
therefore remains `PARTIALLY_VERIFIED`. Reports contain no credential values.
