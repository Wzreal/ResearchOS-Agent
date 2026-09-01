# ResearchOS Agent Phase 9 Plan

**Phase:** Real Integrations and Production Readiness
**Branch:** `feat/phase-9-real-integrations`
**Status:** Architecture plan approved for Phase 9A coding after external-audit
hardening; no Phase 9 production code is part of this document change.

## 1. Repository Findings

- Phase 0-8 are present on the current baseline. The repository has no CLI,
  real provider adapter, HTTP browser, Milvus adapter, or Langfuse adapter.
- `RunConfig` already accepts `OperatingMode.REAL`, but the default
  `RunManager` composition rejects it before persistence.
- Existing provider-facing ports are deliberately different:
  `PlanningModel.generate()` is synchronous, while `Agent.decide()` and
  `VerificationModel.invoke()` are asynchronous.
- Browser, Search, and Local Retrieval already have typed Phase 4 Tool
  contracts. Phase 5 already defines how successful observations become
  Source/Evidence identities.
- Phase 8 owns verification call durability and UNKNOWN outcome semantics.
  Its journal order is PREPARED, DISPATCHED, RESPONSE_COMMITTED.
- Phase 8 already provides the only permitted optional-export path:
  `ObservationRecorder -> ExporterDispatcher -> ObservationExporter`.
- There is no automatic Evidence-to-Claim extraction stage. Phase 9E must add
  a bounded proposal/validation boundary before it can claim query-to-report
  real E2E.
- No test or benchmark result is asserted by this plan.

## 2. Backward Compatibility Audit

### RunConfig and RunState

- Keep both at schema version 1. Do not add credentials, endpoints, provider
  settings, prompt text, embedding dimensions, or vector-store settings.
- Add an injectable real-composition guard to `RunManager`. The existing
  default remains fail-closed for REAL, so existing mock callers and tests keep
  their current behavior.
- A REAL Run receives a separate immutable authority at
  `outputs/<run_id>/real_composition.json`; this avoids changing RunState
  serialization or its config hash.

### Phase 3 checkpoint

- Do not change checkpoint schema, scheduler, operation/attempt identity,
  retry ownership, budget ledger, or settlement rules.
- Real Agent/Tool adapters continue to return the current
  `TaskExecutionResult` and `UsageCertainty` contracts.

### Phase 4 Agent and Tool

- Reuse `Agent`, `Tool`, `CapabilityRegistry`, `AgentRunner`, and stable Tool
  operation keys.
- Registry existence remains distinct from permission. REAL composition builds
  one per-Run registry from the persisted capability pins.
- Source-policy behavior is frozen through a full policy fingerprint. Because
  the current `ToolDescriptor` has no independent policy-fingerprint field and
  changing the operation-key contract would alter Phase 4 semantics, a bounded
  composite operation version is used:

  ```text
  <adapter-version>+p<64-lowercase-sha256>
  ```

  The adapter-version portion is at most 12 characters, making the complete
  value at most 78 characters and therefore valid under the existing 80-byte
  bound. The full hash, not a truncated prefix, is used.

### Phase 5 Evidence and Claim Graph

- Real Browser/Search/Retrieval outputs enter only through
  AgentObservation, EvidenceExtractor, EvidenceObservationIngestor, and the
  existing stores.
- Claim extraction in 9E proposes data only. The existing ClaimGraphService
  and Claim/Edge schemas remain the sole claim authority.

### Phase 6-8 authorities

- VerificationResult, Evaluation artifacts, VerificationOperation, and local
  trace schemas remain authoritative and unchanged except for additive Phase 9
  trace event types where required.
- A provider setting that cannot be proven from an existing artifact remains
  DECLARED/PARTIALLY_VERIFIED in Phase 7. It is never upgraded by assertion.
- For VerificationModel calls, the Phase 8 durable journal always dominates
  any lower-level transport diagnostic. Phase 9A does not reinterpret it.

## 3. Existing Interfaces Reused

- `PlanningModel`: DeepSeek planning and bounded ClaimExtraction model
  transport profiles.
- `Agent`: DeepSeek Agent decision adapter.
- `VerificationModel`: DeepSeek synthesis/Red/Blue/Judge adapter.
- `Tool`: Tavily Search, HTTP Browser, optional Playwright Browser, and hybrid
  retrieval.
- `CancellationSignal`: all async external calls and bounded workers.
- `RunManager`: all Run lifecycle transitions.
- `ClaimGraphService`: all accepted Claim/Edge mutations.
- `EvidenceExtractor` and `EvidenceMemory`: all evidence ingestion.
- `ObservationExporter`: Langfuse only.
- `EvaluationHarness`: all final real-run evaluation.

## 4. Required Additive Contract Changes

### 4.1 REAL composition authority

Add `RealCompositionSnapshot`, `RealCompositionEnvelope`,
`RealCompositionStore`, and `RealCompositionManager`.

`SemanticCompositionPayload` is the sole composition-hash preimage. It is a
strict, versioned, canonical, persistence-safe model containing every semantic
composition field and explicitly excluding:

- `composition_hash`;
- `composition_id`;
- `recorded_at`, artifact hashes, and all other persistence-occurrence
  metadata.

It contains Run ID, Run config hash, enabled modes, per-role model bundles,
capability/adapter/Tool operation pins, source-policy identity and full policy
hash, and the Phase 9C retrieval/index pins when enabled. Identity is computed
without recursion:

```text
composition_hash = sha256(canonical_json(SemanticCompositionPayload))

composition_id = stable_id(
  "realcomp",
  [run_id, run_config_hash, composition_hash],
)
```

`RealCompositionSnapshot` contains the complete semantic fields plus the
derived `composition_hash` and `composition_id`. It is strict,
persistence-safe, and contains:

- schema version, semantic-payload version, snapshot version, Run ID, Run
  config hash;
- `composition_id` and `composition_hash`;
- for each LLM role: provider ID, provider profile, explicit model ID, adapter
  version, exact prompt-content SHA-256, response-schema version, canonical
  base-endpoint SHA-256, endpoint canonicalization version, and model-bundle
  hash, including the safe non-secret credential-slot ID;
- capability ID, Tool/Agent adapter ID, Tool ID where applicable, and exact
  operation version;
- source-policy ID and full canonical policy SHA-256;
- enabled operating modes;
- in 9C, embedding provider/model/adapter version, actual probed dimension,
  embedding bundle hash, Milvus collection/schema hash, corpus/index identity;
- no timestamp or other persistence-occurrence field.

Snapshot construction and every load/compare independently:

1. rebuild `SemanticCompositionPayload` from the stored semantic fields;
2. recompute and compare `composition_hash`;
3. recompute `composition_id` from Run ID, Run config hash, and the verified
   composition hash;
4. compare the stored `composition_id`.

A plausible hash paired with the wrong ID, or an ID derived from a stored hash
that does not match the rebuilt semantic payload, is
`RealCompositionCorruption`. Neither value is trusted merely because it has
valid syntax. Credentials participate in none of these preimages.

`RealCompositionEnvelope` is the persisted authority and contains:

- the complete `snapshot` semantic payload;
- `recorded_at`, created exactly once by the injected Clock for the winning
  first write and never regenerated for equality or resume;
- `artifact_content_hash`, covering the canonical envelope fields other than
  itself, including `recorded_at`, so occurrence-metadata tampering remains
  detectable without changing semantic composition identity.

The complete canonical envelope is bounded to 1 MiB. Composition identity and
equality use both `snapshot.composition_hash` and canonical equality of the
semantic snapshot fields covered by that hash. They never use envelope/model
equality that includes `recorded_at`. A hash match with unequal semantic fields
is corruption, not equality.

Configured endpoints use `provider-base-endpoint-v1`: userinfo, query, and
fragment are forbidden; scheme and host are normalized, the default port is
removed, and the semantic base path is normalized and retained. The safe pin
is the SHA-256 of that complete canonical base endpoint. Thus `/v1` and `/v2`
cannot share a composition pin. Each provider profile owns the fixed resource
path appended beneath the base endpoint; credentials never appear in it.

### 4.1.1 Model-call policy and bundle identity

Every LLM role owns a strict `ModelCallPolicySnapshot` with
`model-call-policy-v1` canonicalization. Its exact canonical fields are:

- policy schema/canonicalization version;
- `max_output_tokens`;
- maximum canonical request bytes and maximum raw/validated response bytes;
- explicit versioned DeepSeek `thinking_mode` and `reasoning_effort`;
- canonical finite-decimal `temperature` and `top_p` only when thinking is
  disabled; thinking-enabled policy records them as ignored and null;
- explicit provider input-token and output-token caps;
- explicit nullable integer `seed` plus a versioned deterministic-generation
  control mode; unsupported controls must be explicitly `None`/`UNSUPPORTED`
  and are never silently omitted from identity;
- connect, read, write, and pool timeout milliseconds;
- cost currency;
- `max_cost_microunits_per_call`;
- immutable pricing/cost-policy ID and version plus a hash of the exact local
  rate/tier/accounting rules used for admission and usage settlement.

All integer bounds are strict positive bounded integers. Decimal generation
parameters use canonical decimal strings rather than binary-float
serialization. Timeout and pricing fields are part of identity even when they
do not change prose: they affect bounded termination, admission, usage
accounting, settlement, and recovery interpretation.

```text
model_call_policy_hash =
  sha256(canonical_json(ModelCallPolicySnapshot))
```

Each role then owns a versioned `ModelBundlePayload` containing exactly:

- bundle schema/canonicalization version;
- provider ID and provider-profile ID;
- explicit model ID;
- adapter ID and adapter version;
- endpoint canonicalization version and canonical safe base-endpoint hash;
- prompt schema/version and exact prompt-content hash;
- response-contract schema/version;
- verified `model_call_policy_hash`.

```text
model_bundle_hash = sha256(canonical_json(ModelBundlePayload))
```

The per-role bundle payload and derived bundle hash are included in
`SemanticCompositionPayload`. Validation first rebuilds and verifies the
policy hash, then rebuilds and verifies the model-bundle hash, then verifies
the composition hash and ID. A stored outer hash cannot hide corrupt or
inconsistent nested policy/bundle identity.

Every `RealIntegrationSettings` field has exactly one ownership category:

| Category | Fields | Resume rule |
|---|---|---|
| A — semantic / resume-frozen | provider/profile/model, adapter ID/version, canonical base endpoint, prompt schema/content, response contract, input/output token caps, DeepSeek thinking/reasoning policy, effective sampling controls, seed and deterministic-generation controls | Any change creates a different bundle/composition and rejects resume or mutation. |
| B — operational but resume-frozen | request/response byte bounds, connect/read/write/pool timeouts, cost currency, per-call cost ceiling, pricing/cost-policy ID/version and exact rules hash | Frozen because each field can change acceptance, termination, admission, accounting, settlement, or recovery behavior. |
| C — intentionally resume-mutable | None for Phase 9A provider-call behavior | No provider-call setting is allowed to change merely because it is labelled operational. Future additions require a separate ADR proving no effect on provider semantics, identity, usage, budgets, or recovery. |
| D — secret / non-semantic | API keys/tokens and derived authorization header values | Values are never persisted or hashed; required secret presence is revalidated, and value rotation is allowed. The non-secret provider-profile/credential-slot identity remains Category A. |

Local diagnostic presentation such as terminal color or doctor output format
is outside `RealIntegrationSettings` and the provider-call path. It may be
mutable only because it cannot affect dispatch, accepted bytes, usage,
budgets, authorities, or recovery.

Store behavior:

- `create(snapshot) -> RealCompositionEnvelope`; the store uses its injected
  Clock only when it wins an absent-authority write, so callers never submit or
  regenerate occurrence metadata;
- first-writer immutable create;
- all filesystem adapter instances share a process-wide lock keyed by the
  pre-resolved store root plus normalized absolute authority path
  (`normcase(abspath(path))`), using the proven Phase 8 path-lock registry and
  reference-count cleanup discipline;
- inside that lock, reload and defensively validate the authority before the
  absence/equality decision;
- if absent, create and atomically persist the first envelope and return it;
- if present with equal semantic snapshot, return the existing envelope
  idempotently, retaining its original `recorded_at` and exact bytes;
- if present with different semantic snapshot, raise
  `RealCompositionConflict`; the losing writer cannot replace the authority;
- same-directory temp, file flush/fsync, atomic replace, and parent-directory
  fsync where supported;
- bounded read, typed parse, semantic hash, envelope content hash, and Run
  identity checks;
- no redaction or silent normalization at the store boundary.

The existence check, comparison, and first write are one critical section.
Atomic replace supplies crash-safe publication but is not treated as CAS by
itself. This guarantees first-writer semantics across adapter instances in the
current single process. Cross-process/distributed CAS remains deferred, and
deployment still permits only one writer process per Run.

### 4.2 Lifecycle and dispatch guard

Add a narrow `RunIntegrationGuard` injected into `RunManager`:

- `validate_create(config)` validates only safe configuration and required
  secret presence before Run persistence;
- `validate_resume(state)` loads and validates the envelope, recomputes the
  current safe semantic snapshot, and compares semantic composition only;
- `validate_bound_state(state)` is the central existing-REAL-Run mutation
  guard;
- `validate_transition(state, target)` adds the process-local binding check for
  `CREATED -> PLANNING` after `validate_bound_state` succeeds.

Ordering is mandatory:

```text
Run CREATED
-> validate complete safe composition
-> first-writer real_composition.json
-> bind per-Run adapters to that exact snapshot
-> allow CREATED -> PLANNING
-> allow first REAL provider dispatch
```

Every real adapter is created through a per-Run `RealIntegrationFactory` only
after `RealCompositionManager.bind()` succeeds. It checks the request Run ID
where that field exists. This prevents external dispatch through the supported
composition path before snapshot authority exists. A read-only lifecycle
authorizer checks the role-specific Run status at construction and immediately
before each call.

`RunManager._load_for_mutation()` is the mandatory centralized path for
`transition()`, `finalize()`, and every future state-mutating method:

```text
load RunState
-> reconcile lifecycle trace
-> if mode is REAL: integration_guard.validate_bound_state(state)
-> return mutation-eligible state
```

`validate_bound_state` requires the authority, validates bounded typed envelope
and both hashes, matches Run ID and persisted Run config hash, recomputes the
current safe semantic composition, and requires semantic hash plus canonical
semantic-field equality. Missing, corrupt, tampered, or mismatched composition
fails before the requested state mutation. This applies in RUNNING,
VERIFYING, terminal finalization, and all other states, not only at
`CREATED -> PLANNING`. The latter additionally requires the current process to
have bound its adapters to the validated snapshot.

Resume performs the same safe semantic comparison and then establishes the
process-local binding needed for later dispatch. It never regenerates or
compares `recorded_at`. Credential values may rotate because they are excluded
from semantic identity. Provider/profile/model/canonical base endpoint/adapter/
prompt content/response schema/capability/source policy/retrieval changes fail
closed.

`resume()` performs this guard and binding after lifecycle reconciliation but
before appending `run.resumed`. The read-only `load()` contract remains
unchanged; only resume and mutation boundaries assert compatibility with the
currently selected REAL integrations.

A missing snapshot can be initialized only when all of these hold:

- REAL Run is `CREATED` at revision 0;
- no checkpoint, evidence, claims, verification authority, verification
  operation, or claim-extraction artifact exists;
- trace contains only lifecycle creation records and no planning, Agent, Tool,
  evidence, claim, verification, or provider-dispatch event.

Otherwise it is `RealCompositionMissing` and requires operator intervention.

### 4.3 Provider transport diagnostics

Add adapter-level `ProviderDispatchDiagnostic` with
`NOT_DISPATCHED`, `DISPATCHED_OUTCOME_UNKNOWN`, and `RESPONSE_RECEIVED`.
It may classify startup probes, Planning/Agent diagnostics, and Tool calls.

It does **not** own or redefine Phase 8 state. Once Phase 8 executes
`journal.mark_dispatched()`:

- a Verification adapter ConnectError cannot downgrade the call;
- the adapter cannot retry;
- absence of a committed validated response remains INTERRUPTED_UNKNOWN with
  UNKNOWN usage;
- changing this rule requires a separate Phase 8 semantic audit.

### 4.4 Retrieval and Claim Extraction ports

Add `EmbeddingProvider`, `VectorIndex`, and `RetrievalIndexStore` for 9C.

Add for 9E:

- `ClaimExtractionModel`
- `ClaimExtractionPolicy`
- `ClaimExtractionRequest`
- `ClaimProposal` and `ProposedEvidenceRelation`
- `ClaimExtractionResult`
- `ClaimExtractionService`

These contracts do not create a second claim store or runtime.

## 5. Selected Provider Architecture

```text
PlanningModel / Agent / VerificationModel / ClaimExtractionModel
                              |
             role-specific versioned prompt builder
             + strict typed response validation
                              |
              OpenAICompatibleChatTransport
                              |
                  DeepSeekProviderProfile
```

- Use raw HTTPX clients rather than an LLM SDK so one dispatch, byte limits,
  redirect policy, timeout classification, and no retry are explicit.
- Planning remains synchronous because its existing port is synchronous;
  Agent, Verification, and Claim Extraction use async paths where their ports
  permit it.
- DeepSeek is the first real LLM profile. MiMo is deferred until the DeepSeek
  transport/contract suite passes; it will be a profile, not another runtime.
- Prompt identity binds two values:
  - human-readable prompt schema/version;
  - SHA-256 of the exact immutable static prompt content plus the canonical
    response schema supplied to the model.
- Model identity is the validated `ModelBundlePayload` hash defined in 4.1.1,
  not an adapter-supplied opaque label. Its preimage binds provider/profile,
  model, adapter, canonical base endpoint, prompt, response contract, and the
  complete `ModelCallPolicySnapshot` hash.
- Runtime inputs are not part of prompt-content identity. Phase 8 separately
  pins each verification request's canonical request hash.
- Raw prompts and raw provider envelopes remain invocation-local.

## 6. Phase 9A Plan

### 6.1 Settings and optional imports

- Add immutable non-persistent `RealIntegrationSettings` loaded from a
  `SecretSource`; first implementation reads the process environment.
- Require every Category A/B value in 4.1.1 explicitly: provider/profile/model
  and adapter identity, canonical base endpoint, prompt/response versions,
  output-token and request/response bounds, generation controls, all four
  timeouts, currency, per-call cost ceiling, and immutable pricing-policy
  identity/rules hash. Defaults, where supported, are resolved before snapshot
  construction and the effective value is persisted in the semantic policy;
  provider/server defaults are never an implicit resume input.
- Optional adapter modules import third-party packages lazily. Constructing an
  adapter without its extra raises a typed error naming the required extra.
- Importing `researchos` and all core Phase 0-8 modules must not import HTTPX,
  trafilatura, pymilvus, OTel, or Playwright.

### 6.2 Dependency groups

Plan the following exact groups in `pyproject.toml`:

```toml
[project.optional-dependencies]
llm = ["httpx>=0.28.1,<1.0"]
search = ["httpx>=0.28.1,<1.0"]
browser = [
  "httpx>=0.28.1,<1.0",
  "trafilatura>=2.2,<3.0",
]
retrieval = [
  "httpx>=0.28.1,<1.0",
  "pymilvus==2.6.14",
]
observability = [
  "httpx>=0.28.1,<1.0",
  "opentelemetry-proto>=1.36,<2.0",
]
browser-js = ["playwright>=1.55,<2.0"]
real = [
  "httpx>=0.28.1,<1.0",
  "trafilatura>=2.2,<3.0",
  "pymilvus==2.6.14",
  "opentelemetry-proto>=1.36,<2.0",
]
```

`real` is the baseline production bundle and deliberately excludes
`browser-js`; Playwright remains an explicit opt-in.

### 6.3 Composition snapshot and Run gate

- Implement semantic snapshot, occurrence envelope, shared-path-lock
  store, and manager before any provider adapter.
- Inject the REAL guard into RunManager without changing its default behavior;
  route every existing/future Run mutation through the guarded
  `_load_for_mutation()` boundary.
- Test create -> first-writer envelope -> process-local adapter binding ->
  planning ordering; semantic resume equality independent of occurrence time;
  credential rotation; semantic setting mismatch; missing/tampered authority;
  mutation of an unbound/mismatched existing REAL Run; and attempts to
  construct/dispatch an unbound adapter.

### 6.4 DeepSeek adapters

- One OpenAI-compatible transport with explicit sync/async entry points and no
  retry middleware.
- Three role adapters in 9A: Planning, Agent decision, Verification.
- Require non-streaming structured JSON, strict provider/model identity,
  bounded raw response, and provider usage validation.
- Phase 9A fails closed when REAL capabilities are non-empty and exposes only
  final/failed Agent decisions. Binding exact per-Run capability input schemas
  is a Phase 9B prerequisite; model content cannot grant permission.
- Verification exceptions after Phase 8 durable DISPATCHED are allowed to
  propagate to the existing UNKNOWN handling without transport downgrade.

### 6.5 9A acceptance

- Core users can install/import without extras.
- All-extra offline tests cover the real adapters through fake transports.
- A REAL Run cannot enter PLANNING or dispatch until its composition snapshot
  is durable and matched.
- Every mutation of an existing REAL Run validates its composition authority;
  `transition()` and `finalize()` cannot bypass this through direct state load.
- Same Run cannot resume under a changed model, canonical base endpoint,
  adapter, prompt content, response schema, model-call policy, capability, or
  source policy.
- Credential rotation alone does not change composition identity.
- Concurrent same-composition creates converge on the original envelope;
  concurrent different-composition creates have exactly one winner and the
  loser cannot overwrite it.
- No provider call is retried by transport/adapter.
- No raw provider data or secret crosses a persistence boundary.

## 7. Phase 9B Plan

### Tavily

- Implement the existing Search Tool contract with `include_answer=false`,
  `include_raw_content=false`, `auto_parameters=false`, bounded results, and
  usage reporting.
- Tavily answer is ignored even if returned unexpectedly. Only validated hits
  become Agent observations and Phase 5 evidence.
- Oversize or malformed hits reject the response; content is not truncated
  while retaining a full-content identity.

### HTTP Browser and SSRF

- Baseline is HTTPX plus trafilatura, with manual redirects and a pinned-DNS
  transport preserving original Host/SNI.
- Reject non-HTTP(S), userinfo, unsafe ports, localhost, loopback, private,
  link-local, reserved, multicast, and unspecified addresses.
- Re-resolve/revalidate every redirect and connect only to the validated IP.
- Disable environment proxy, `.netrc`, automatic redirects, cookies, and
  authorization forwarding.
- Cap DNS answers, redirects, raw bytes, decoded bytes, extraction bytes,
  content type, and time. Reject non-identity content encoding in the baseline.
- Playwright is a separate explicitly selected adapter, never silent fallback
  and never unrestricted computer use.

## 8. Phase 9C Plan

- Implement an OpenAI-compatible Qwen text-embedding adapter with bounded
  batches and responses.
- Probe actual dimension; never infer it from a model name. Doctor is
  read-only/zero-cost by default, so the paid probe requires `--probe-paid`.
- Pin Milvus Standalone `v2.6.17` with `pymilvus==2.6.14`.
- Validate collection schema, actual vector dimension, embedding bundle,
  corpus hash, and index manifest before retrieval.
- Store a canonical atomic index manifest under
  `outputs/indexes/<collection>/index_manifest.json`.
- Combine existing BM25 and dense ranks using deterministic RRF with
  `rrf_k=60`, Decimal arithmetic, and `(document_id, chunk_id)` tie-break.
- Existing `LocalRetrievalResult` remains the Tool output. Reranker/MMR is
  optional post-baseline hardening.

## 9. Phase 9D Plan

- Implement Langfuse strictly as `ObservationExporter` over one-shot OTLP/HTTP.
- Reuse Phase 8's nonblocking dispatcher and local-only diagnostics.
- No exporter retry, durable remote queue, business await, raw prompt/body
  export, or authority role.
- Extend defensive secret-key recognition and run actual-secret canary scans
  against state, trace, checkpoint, composition, verification, evaluation,
  doctor, and exception output.
- REAL production composition disables the trusted local Python Tool by
  default. Enabling it requires explicit development acknowledgement and does
  not claim hostile-code isolation.

## 10. Phase 9E Plan

### 10.0 Application sequencing owner

Add a thin `ResearchWorkflowCoordinator` in the application layer. It is the
single composition point for the controlled production flow:

```text
RunManager lifecycle
-> PerspectivePlanner
-> AsyncDAGExecutor through the existing backend
-> existing Evidence ingestion boundary
-> ClaimExtractionService
-> DurableVerificationCoordinator / VerificationService
-> EvaluationHarness
-> RunManager lifecycle mapping/finalization
```

The coordinator invokes existing typed services and maps their already-defined
terminal results to legal Run transitions. It is not a scheduler, Agent
runtime, retry loop, checkpoint manager, budget ledger, Claim authority,
verification coordinator, correctness service, or evaluator. Phase 3 retains
execution/retry/checkpoint/budget ownership; Phase 8 retains durable
verification orchestration; Phase 5-7 retain their authorities. `cli.py` only
parses bounded input/configuration, calls this application API, and renders its
typed result. Business sequencing must not be implemented directly in the CLI.

### 10.1 Bounded automatic Claim Extraction

The production flow is:

```text
committed Evidence snapshot
-> freeze current evidence and compute extraction identity
-> ClaimExtractionModel (one bounded call)
-> ClaimExtractionService structural validation
-> existing ClaimGraphService mutations
-> Phase 6/8 Verification
-> Phase 7 Evaluation
```

`ClaimExtractionPolicy` fixes:

- `max_model_calls = 1`;
- max evidence items and whole-item context bytes;
- max response bytes;
- max claims;
- max evidence references per claim;
- absolute deadline and cancellation;
- per-call token and cost upper bounds.

The model may propose only:

- a task-local `proposal_key` SafeId;
- one atomic nonblank statement;
- typed evidence references containing evidence ID, exact current revision,
  expected content hash, and relation.

Application validation:

- whole Evidence items only; no truncated content with full identity;
- exact Run/snapshot/request identity;
- proposal keys unique and count-bounded;
- every evidence ID/revision/content hash is current and belongs to the frozen
  snapshot;
- every proposal has at least one accepted evidence reference;
- only existing ClaimEvidenceRelation values are accepted;
- dangling, unsupported, stale, duplicated, or cross-Run references reject the
  proposal before any Claim mutation;
- statement atomicity receives only bounded structural validation; factual
  truth remains Verification's concern.

Claim scope is application-owned and stable:

```text
claim_scope_key = stable_id(
  "scope",
  ["claim-extraction-v1", extraction_id, proposal_key],
)
```

Model prose is not used directly as Claim identity. Accepted mutations use the
existing ClaimGraphService, which remains the only Claim/Edge authority.

Publish a bounded immutable `claim_extraction.json` containing the frozen
input hash, validated proposals, usage/certainty, model bundle, and content
hash before applying idempotent graph mutations. Recovery replays those exact
validated proposals through ClaimGraphService with zero model calls. It is a
proposal/replay artifact, not a second Claim authority or a scheduler.

A provider failure before this proposal artifact is committed is not
transparently retried. UNKNOWN usage remains visible and the Run terminates
according to the existing lifecycle integration policy.

### 10.2 Controlled real query-to-report E2E

- Real Planning -> Phase 3 runtime -> real Agent/Tools -> Evidence ingestion ->
  automatic Claim Extraction -> ClaimGraph -> durable Verification ->
  Evaluation.
- Uses a versioned local corpus, real DeepSeek, real Qwen embedding, and real
  Milvus.
- Manual claim/edge fixtures are allowed only in deterministic unit/integration
  tests and are not used by the final controlled real E2E.
- A separate live-web smoke adds Tavily and HTTP Browser but is not a stable
  benchmark.
- Measured results are generated only from actual Run/Evaluation artifacts; an
  unexecuted experiment is recorded as `NOT RUN`.

## 11. Retry / Idempotency Semantics

### General adapters

- Local validation, composition mismatch, source-policy rejection, and doctor
  configuration checks occur before dispatch.
- Agent/Tool transport diagnostics may distinguish provable NOT_DISPATCHED
  from unknown post-dispatch failure for logging and existing upper-layer
  policy.
- HTTP response status is a received outcome; 429/5xx may be marked retryable
  for Phase 3 policy but is never retried inside the adapter.
- Write/read timeout, reset, cancellation, or malformed response after request
  handoff is not silently retried.

### Verification override

```text
Phase 8 PREPARED
-> Phase 8 durable DISPATCHED
-> adapter invocation
-> no committed validated response
= INTERRUPTED_UNKNOWN
```

Transport ConnectError, DNS diagnostics, or local exception after the durable
DISPATCHED marker cannot convert this to NOT_DISPATCHED. Phase 9A does not
change `VerificationCoordinator` exception handling.

### Claim extraction

- Maximum one model call per extraction invocation.
- Same committed claim-extraction artifact replays with zero model calls.
- Existing ClaimGraph receipts/identities make applying the same accepted
  proposal idempotent.
- There is no Claim-extraction retry loop, second budget ledger, or checkpoint.

## 12. Browser / SSRF Security Model

- URL canonicalization and DNS/IP policy execute before each connection and
  redirect.
- Actual socket connection is pinned to the validated address while TLS uses
  the original hostname.
- Mixed public/private DNS answers fail closed rather than selecting only the
  public subset.
- Redirect-to-private, DNS rebinding, IPv4-mapped IPv6, numeric/encoded host,
  zone IDs, proxy inheritance, decompression/body bombs, unsupported content
  type, and cancellation all have negative tests.

## 13. Secret Management

- Credentials remain outside RunConfig and every durable identity.
- SecretSource returns in-memory secret values; only presence is validated.
- Safe snapshot pins contain canonical safe base-endpoint hashes including the
  semantic path, never URL query/userinfo or headers.
- Credential rotation is compatible only if all semantic safe pins remain
  equal.
- Provider exception strings are replaced by stable sanitized codes.

## 14. `researchos doctor --real` Design

Default doctor is zero-cost and read-only:

- parse/validate configuration and secret presence;
- recompute safe composition and compare an optional Run snapshot;
- validate endpoint syntax, DNS/TLS reachability where read-only;
- use read-only model-list/metadata endpoints when supported;
- inspect Tavily account/usage only through a documented read endpoint;
- run Browser policy self-tests without fetching arbitrary pages;
- inspect existing Milvus connection, collection schema, and index manifest;
- validate Langfuse configuration/auth using a read-only endpoint if available;
- run secret/redaction canaries.

Flags:

- `--probe-paid`: permits a bounded LLM/embedding/search request and reports
  its expected cost bound before dispatch.
- `--probe-writes`: permits a bounded remote write such as a Langfuse test
  observation.

Without these flags, a check that cannot establish model availability or
dimension without a paid call returns PARTIALLY_VERIFIED, not PASS or FAIL.
Doctor never creates a Run, collection, index, fallback adapter, or remote
trace by default.

## 15. Integration Test Strategy

### Core CI job

```text
uv sync
uv run python -c "import researchos"
uv run pytest --basetemp=.pytest_tmp -p no:cacheprovider \
  -m "not requires_real_extra"
uv run ruff check .
git diff --check
```

- No optional packages, credentials, services, browser binary, or network
  access during tests.
- Every Phase 0-8 test and every Phase 9 settings/composition/lifecycle test is
  core and must run in this job; none may carry `requires_real_extra`.
- Only tests that actually instantiate a third-party real adapter may use the
  registered `requires_real_extra` marker. Core still tests its typed
  missing-extra failure without that dependency.
- Adds import tests proving optional adapter modules are not imported from the
  root/core package. CI asserts that the core selection is nonempty and that
  no Phase 0-8 test was deselected, so plain `uv sync` demonstrates the full
  existing system rather than only root imports/contracts.

### All-extras offline CI job

```text
uv sync --all-extras
uv run pytest --basetemp=.pytest_tmp -p no:cacheprovider
uv run ruff check .
git diff --check
```

- Uses fake HTTP transports/resolvers, fake Milvus, fake OTel receiver, and
  deterministic model fixtures.
- Does not require API keys, a Milvus server, a browser binary, Langfuse, or
  network access during tests.
- Playwright unit tests inject a fake launcher and never launch a browser.

### Opt-in real integration tests

- Require `RESEARCHOS_RUN_REAL_INTEGRATION=1` and `integration` marker.
- Missing provider-specific credentials produce an explicit skip, never mock
  fallback.
- Paid and write probes require separate explicit environment gates matching
  doctor flags.
- Every test has call, response-byte, token, cost, and deadline bounds.
- No exact prose, live ranking, or tight wall-clock assertion.

Required new regression cases include:

- `composition_hash` exactly equals the canonical
  `SemanticCompositionPayload` hash and excludes both derived identity fields;
- a syntactically valid composition hash paired with a wrong composition ID is
  corruption;
- a composition ID correctly derived from a stored hash whose semantic payload
  recomputes to another hash is corruption;
- nested model-call policy or model-bundle hash mismatch is corruption even if
  an attacker replaces only an outer derived value;
- parametrized changes to max output tokens, request/response bounds,
  temperature, top-p, seed/control mode, each timeout, currency, per-call cost,
  pricing-policy version, or exact rules hash change policy/bundle/composition
  identity and reject resume/mutation;
- rotating only credential values preserves all hashes and permits
  resume/mutation after secret-presence validation;
- concurrent same semantic-snapshot writers return one identical envelope and
  retain the winning `recorded_at`;
- concurrent different semantic-snapshot writers produce exactly one authority
  winner, and the losing writer raises `RealCompositionConflict` without
  overwrite;
- equivalent authority paths with different spelling/case normalization use
  one shared lock rather than two independent critical sections;
- recomputing an equal semantic snapshot at a later Clock time is idempotent
  and resume does not compare occurrence metadata;
- transition/dispatch before snapshot rejected;
- RUNNING with a deleted composition rejects transition and finalize;
- VERIFYING with tampered envelope/hash rejects every mutation;
- changed model, prompt content, canonical endpoint path, adapter, or policy
  rejects mutation even when callers never invoke `resume()`;
- resume after credential rotation accepted;
- unchanged composition with rotated credentials permits transition/finalize;
- resume after prompt-content/model/base-endpoint path/policy change rejected;
- missing snapshot only initialized in pristine CREATED state;
- Verification ConnectError after durable DISPATCHED remains UNKNOWN;
- core import with every optional dependency unavailable;
- plain-`uv sync` core CI runs the complete Phase 0-8 suite plus Phase 9
  composition/settings/lifecycle tests, while only true extra-dependent tests
  are deselected;
- prompt text change alters bundle/composition hash;
- full policy fingerprint alters bounded operation version;
- doctor default makes zero paid calls and zero writes;
- bounded Claim extraction rejects dangling/stale/cross-Run evidence;
- Claim extraction replay uses zero model calls and idempotent graph mutation;
- `ResearchWorkflowCoordinator` sequences existing services while the CLI owns
  no business-stage transition, retry, checkpoint, or verification logic;
- controlled real E2E has no claim/edge fixture injection.

## 16. Real E2E Strategy

- Controlled E2E is the final 9E query-to-report path and includes automatic
  Claim Extraction.
- Deterministic fixture-based claim injection remains a lower-level test only.
- Live web smoke records execution time/provider/source volatility and cannot
  become a deterministic benchmark.
- Evaluation metrics and costs come from actual artifacts. Missing or
  unavailable values are never encoded as zero.

## 17. Migration Strategy

- No RunState, RunConfig, Phase 3 checkpoint, Evidence, Claims,
  VerificationResult, EvaluationRun, or VerificationOperation schema bump.
- `real_composition.json` is an additive authority with its own schema/version,
  byte bound, semantic `RealCompositionSnapshot`, and occurrence
  `RealCompositionEnvelope`.
- Its identity chain is versioned and non-circular:
  `ModelCallPolicySnapshot -> ModelBundlePayload ->`
  `SemanticCompositionPayload -> composition_hash -> composition_id`.
  Load/bootstrap migrations must recompute and verify each layer independently;
  no stored derived hash or ID is trusted as a preimage field.
- The first persisted envelope owns `recorded_at` permanently. Resume and
  idempotent create compare only the validated semantic snapshot; no migration
  may regenerate occurrence metadata or turn it into identity.
- Old MOCK Runs require no composition snapshot and load unchanged.
- Historically no valid REAL Run could pass default Phase 1 creation, so no
  existing REAL artifact is silently grandfathered.
- A missing REAL composition snapshot is bootstrap-compatible only for a
  pristine revision-0 CREATED Run with no dispatch evidence; every other case
  fails closed.
- Prompt/profile/adapter/model-call-policy/canonical base-endpoint changes
  produce a new composition and cannot resume or mutate an existing Run.
- Credentials may rotate without migration.
- Milvus corpus/model/dimension/schema changes require a new versioned
  collection or explicit rebuild; no in-place guessing.
- `claim_extraction.json` is additive. Claims/edges themselves remain in the
  existing ClaimGraph authority and use existing revision semantics.
- Old evaluation authorities retain their provenance; Phase 9 does not rewrite
  them with newly declared provider pins.

## 18. Exact Files To Add / Modify

### Add

```text
src/researchos/configuration/__init__.py
src/researchos/configuration/real_settings.py
src/researchos/configuration/environment.py
src/researchos/configuration/validation.py

src/researchos/domain/real_composition.py
src/researchos/domain/claim_extraction.py

src/researchos/interfaces/providers.py
src/researchos/interfaces/retrieval.py
src/researchos/interfaces/claim_extraction.py

src/researchos/application/real_composition.py
src/researchos/application/integration_factory.py
src/researchos/application/retrieval_indexer.py
src/researchos/application/claim_extraction.py
src/researchos/application/research_workflow.py
src/researchos/application/doctor.py

src/researchos/adapters/real_composition_filesystem.py
src/researchos/adapters/real_composition_memory.py
src/researchos/adapters/openai_compatible.py
src/researchos/adapters/deepseek.py
src/researchos/adapters/mock_claim_extraction.py
src/researchos/adapters/claim_extraction_filesystem.py
src/researchos/adapters/tavily.py
src/researchos/adapters/http_browser.py
src/researchos/adapters/qwen_embedding.py
src/researchos/adapters/milvus.py
src/researchos/adapters/hybrid_retrieval.py
src/researchos/adapters/langfuse_observability.py
src/researchos/adapters/playwright_browser.py  # only if optional 9B adapter ships

src/researchos/cli.py

tests/test_phase9_real_composition.py
tests/test_phase9_settings.py
tests/test_phase9_llm_adapters.py
tests/test_phase9_search_browser.py
tests/test_phase9_retrieval.py
tests/test_phase9_observability.py
tests/test_phase9_claim_extraction.py
tests/test_phase9_doctor.py
tests/test_phase9_integration.py
tests/fixtures/phase9/controlled_corpus.jsonl
tests/fixtures/phase9/controlled_dataset.json

deploy/milvus/docker-compose.yml

docs/PHASE9_DESIGN.md
docs/PHASE9_SECURITY_REVIEW.md
docs/PHASE9_OPERATOR_RUNBOOK.md
docs/PHASE9_RELEASE_CHECKLIST.md
docs/PHASE9_MEASURED_RESULTS.md
```

### Modify

```text
src/researchos/interfaces/lifecycle.py
src/researchos/application/run_manager.py
src/researchos/application/errors.py
src/researchos/domain/contracts.py             # additive trace event types only
src/researchos/security/redaction.py
src/researchos/adapters/local_retrieval.py      # extract reusable BM25 core only
src/researchos/__init__.py

tests/test_configuration.py
tests/test_local_retrieval.py
tests/test_redaction.py

pyproject.toml
uv.lock
.env.example
.github/workflows/ci.yml
README.md
docs/PROJECT_SPEC.md
docs/ARCHITECTURE.md
docs/TASKS.md
docs/DECISIONS.md
docs/EVALUATION.md
```

During this architecture-hardening turn, only `PLAN9.md` is modified.

## 19. Risks / Deferred

- Existing Planning port is synchronous and its provider usage is not settled
  by the Phase 3 durable ledger. Calls remain bounded, and the limitation must
  be published in 9E results.
- Claim Extraction is bounded and replayable but is not a Phase 3 task or a
  second checkpoint. An unknown call before its immutable proposal artifact is
  committed is not automatically retried.
- Some Agent/Search/Browser/Retrieval SUT pins remain only declared or
  partially verified by the current Phase 7 artifact reader.
- REAL composition first-writer locking and Phase 8 filesystem CAS remain
  single-process. Deployment requires one writer process per Run;
  cross-process/distributed CAS is deferred.
- Unknown provider outcomes require operator intervention.
- Live web results are not reproducible.
- Playwright is not a security sandbox; local Python remains trusted-code only
  and is disabled in the default REAL profile.
- Reranker/MMR, distributed Milvus, durable remote export, cross-process
  leases, and generic provider-side deduplication remain deferred.

## 20. P0 / P1 / P2 Concerns

### P0

- 0.

### P1

- 0.
- Circular composition identity: closed by an explicit
  `SemanticCompositionPayload` preimage excluding both derived fields, followed
  by independently recomputed `composition_hash` and `composition_id`; nested
  policy and bundle hashes are validated before the outer identity.
- Behavior-affecting REAL settings ownership: closed by the versioned
  `ModelCallPolicySnapshot`, explicit `ModelBundlePayload` preimage, exhaustive
  A/B/C/D classification, and zero resume-mutable provider-call settings in
  Phase 9A.
- Semantic equality versus occurrence metadata: closed by separating
  `RealCompositionSnapshot` from `RealCompositionEnvelope`, creating
  `recorded_at` only for the winning write, and comparing only validated
  semantic fields/hash on idempotent create and resume.
- First-writer concurrency: closed by a normalized per-authority-path shared
  process lock with reload/check/write inside one critical section; atomic
  replace is used only for crash-safe publication.
- Complete REAL mutation gating: closed by mandatory
  `_load_for_mutation() -> validate_bound_state()` validation for transition,
  finalize, and future mutation paths, with an additional process-local bind
  gate for `CREATED -> PLANNING`.
- Phase 8 dispatch conflict: closed by explicit journal precedence and no
  Phase 9A coordinator change.
- False E2E claim: closed by bounded automatic Claim Extraction in 9E.
- Optional dependency isolation: closed by exact extras, lazy imports, and
  separate core/all-extras CI jobs.

### Non-blocking P2

All re-audit P2 design/cleanup gaps are closed:

- Endpoint semantic pin: option B is locked. The composition pins the complete
  `provider-base-endpoint-v1` hash including its semantic path and excluding
  forbidden userinfo/query/fragment; `/v1` and `/v2` are distinct.
- Phase 9E sequencing ownership: the thin application-layer
  `ResearchWorkflowCoordinator` composes existing lifecycle/planning/runtime/
  evidence/extraction/verification/evaluation services; CLI remains an adapter
  and no second runtime or coordinator is introduced.
- Secret documentation consistently names canonical safe base-endpoint hashes,
  including semantic paths, rather than origin-only hashes.
- The plain-`uv sync` core job runs all Phase 0-8 tests and the Phase 9 core
  composition/settings/lifecycle suite; only tests that truly instantiate an
  optional third-party adapter are marked out, while missing-extra behavior is
  still tested in core.

Remaining non-blocking operational limitations are:

- Planning usage lacks durable Phase 3 settlement.
- Some real-adapter SUT pins remain provenance-downgraded in Phase 7.
- Claim Extraction unknown-before-authority cannot be transparently resumed.
- Cross-process REAL composition CAS is not provided; deployment remains
  single-process writer per Run.

These are documented operational limitations and do not block the isolated
Phase 9A implementation. Blocking P2 for Phase 9A: 0.

## 20.1 Phase 9A provider-aware audit locks

- The canonical DeepSeek base endpoint is `https://api.deepseek.com`.
  Planning/Verification use `deepseek-v4-pro`; Agent uses
  `deepseek-v4-flash`.
- Each role freezes and explicitly sends thinking mode and reasoning effort.
  The default is thinking enabled with high effort. Ignored sampling controls
  are canonical nulls and do not create misleading semantic variance.
- Pricing identity is per model and versioned. It stores conservative input and
  output rate upper bounds. Provider-reported token counts remain measured,
  but locally calculated monetary cost is always `UPPER_BOUND`; cache fields
  cannot reduce it and missing authoritative billing never becomes exact zero.
- `max_input_tokens` is a post-response integrity cap, not a pre-dispatch token
  estimate. Request bytes remain only a transport bound. A versioned,
  adapter-owned model safety profile supplies the independent input-token
  reservation; combined input/output token and cost reservations must fit the
  Run budget and Agent task hard limits through existing ownership boundaries.
- Only `finish_reason=stop` is successful. Other known reasons map to stable
  response-received failures, preserve validated usage, and never trigger an
  adapter retry.
- Pricing safety profile version/hash and the derived provider-call reservation
  are frozen into policy, bundle, and composition identity. Supported model
  aliases are explicit; operator rates may increase but cannot fall below the
  adapter minima.
- The reviewed v2 DeepSeek safety profile pins the current 1M context window,
  384K provider output maximum, and USD billing currency. It reserves the full
  context as input without claiming local tokenization, rejects non-USD policy,
  and is versioned safety data rather than a permanent pricing guarantee.
- Every supported dispatch reloads RunState and durable composition, validates
  role/status and artifact integrity, recomputes current semantics, and matches
  the process-local binding immediately before HTTP.
- Transient Agent failure metadata survives UNKNOWN usage into Phase 3; no
  adapter retry is added. Generic secret sources reject absent/blank values,
  and failed owned-client close is not recorded as successful.

## 21. GO / NO-GO

**GO for Phase 9A Coding.**

Phase 9A is limited to:

1. ADR/design synchronization;
2. optional dependency structure and core/all-extras CI split;
3. safe non-persistent settings and secret source;
4. versioned `ModelCallPolicySnapshot` and non-circular model-bundle/
   composition identity, durable `RealCompositionSnapshot`/
   `RealCompositionEnvelope`, store/manager, shared-path first-writer protocol,
   central existing-Run mutation guard, and per-Run adapter binding;
5. OpenAI-compatible transport and DeepSeek Planning/Agent/Verification
   adapters;
6. zero-cost/read-only doctor basics;
7. complete offline tests for those boundaries.

Phase 9A must not begin Tavily, Browser, retrieval, Langfuse, Claim Extraction,
or E2E implementation.

```text
P0 = 0
P1 = 0
Blocking P2 = 0
```
