# ResearchOS Phase 9B Plan

## 1. Status, goal, and ownership

Phase 9B adds REAL Tavily Search and a bounded HTTP Browser, integrates them
through the Phase 4 Agent/Tool/CapabilityRegistry boundary, and sends eligible
Browser observations through the existing Phase 5 Evidence ingestion path.

Verified baseline:

```text
branch: feat/phase-9b-search-browser
HEAD: 9fe8791883063863cd0a282e8de2e23f7728654a
Phase 9A ancestor: PASS
PLAN9.md and docs/PHASE9_DESIGN.md: present
```

Ownership remains unchanged:

- Phase 3 owns task-attempt reservation, retry, timeout, settlement, and
  checkpoints.
- Phase 4 owns Agent/Tool execution, CapabilityRegistry authorization, and
  Tool operation identity.
- Phase 5 owns Source/Evidence identity, revision, receipt, and persistence.
- Phase 9A owns immutable REAL composition and lifecycle dispatch authority.
- Phase 9B adapters own only provider transport, bounded parsing, and their
  frozen policy contracts.

No second runtime, retry loop, budget ledger, permission store, Evidence store,
or REAL-to-MOCK fallback is introduced.

## 2. Architecture

```text
Frozen REAL settings
        -> RealCompositionManager
        -> RealIntegrationFactory
             +-> tool-capable DeepSeekAgent
             +-> CapabilityRegistry
                   +-> TavilySearchTool
                   +-> BoundedHttpBrowserTool
        -> AgentRunner
        -> AgentObservation
        -> EvidenceExtractor v2
        -> EvidenceObservationIngestor
        -> EvidenceStore
```

## 3. Tavily contract

REAL descriptor:

```text
capability_id     = web_search
tool_id           = tavily_search
adapter_id        = tavily_http
adapter_version   = tavily-http-v1
operation_version = 1+p<64-character-policy-hash>
mode              = REAL
input_type        = SearchRequest
output_type       = SearchResultV2
side_effect       = EXTERNAL
idempotency       = IDEMPOTENT
```

The adapter performs at most one HTTP request. Phase 3 alone may later retry
the task after the normal idempotency and failure-policy checks.

### 3.1 Standard provider profile

Define a frozen `TavilyProviderFeatureProfile`. Phase 9B implements only:

```text
STANDARD.safe_search = false
```

An optional future enterprise profile may use `safe_search=true`, but it is not
selectable in Phase 9B. A feature-profile change must change the Tavily policy
hash, capability pin, operation version, and REAL composition.

### 3.2 Provider request versus internal policy

`TavilySearchPolicySnapshot` contains:

1. `TavilyProviderRequestPolicyV1`: only documented Tavily `POST /search`
   fields that are serialized to HTTP.
2. `TavilyResearchOSPolicyV1`: internal bounds, identities, and disabled
   features that are never serialized to Tavily.

The standard provider request freezes and explicitly sends:

```text
search_depth               = basic
chunks_per_source          = 1
topic                      = general
max_results                = 5, configurable within 1..20
auto_parameters            = false
exact_match                = false
safe_search                = false
include_answer             = false
include_raw_content        = false
include_images             = false
include_image_descriptions = false
include_favicon            = false
include_usage              = true
include_domains            = []
exclude_domains            = []
country                    = null
time_range                 = null
start_date                 = null
end_date                   = null
```

`language` and `filter_by_language` are not provider-request fields and must
not appear in the HTTP JSON. The internal ResearchOS policy pins language
filtering as unsupported and disabled.

Dynamic query and requested result count come from existing `SearchRequest`.
A result count above the frozen maximum fails without clamping or dispatch.
No undocumented or internal field is sent.

The internal policy also pins:

- provider/profile/feature-profile identity;
- adapter and provider schema versions;
- canonical safe endpoint hash;
- query character and UTF-8 byte bounds;
- request/header/response byte bounds;
- connect/read/write/pool/total timeouts;
- URL canonicalization and duplicate handling versions;
- stable provider-error mapping;
- credential slot ID, never the secret;
- credit/pricing policy and immutable provider reservation;
- unsupported language filtering as an internal disabled feature.

Every semantic change propagates through:

```text
policy_hash -> CapabilityCompositionPin -> operation_version
            -> REAL composition hash and ID
```

### 3.3 HTTPS credential transport

The standard profile requires exactly the canonical base endpoint:

```text
https://api.tavily.com
```

The adapter appends fixed path `/search`. Endpoint validation occurs before
credential lookup and before HTTP request construction.

Required properties:

- HTTPS only;
- no userinfo, query, fragment, custom path, or non-default port;
- frozen canonical endpoint hash;
- no redirects;
- `trust_env=False`;
- no proxy, netrc, or credential forwarding.

Bearer credentials are never accessed or transmitted for an invalid or HTTP
endpoint. Composition, traces, errors, and diagnostics contain only the
credential slot ID and safe endpoint hash.

### 3.4 Response

An adapter-private strict response schema enforces bounded results, required
canonical HTTP(S) URLs, bounded title/snippet, finite optional score, unique
canonical URLs, provider array rank, and bounded credit usage. Provider request
IDs are retained only as safe hashes.

Unexpected provider answer, raw content, images, or error prose cannot enter
Tool output, Evidence, trace, or report. Only validated `SearchHitV2` records
are returned.

## 4. Provider sub-operation reservations

Define immutable `ProviderSuboperationReservation`:

```text
schema_version
reservation_version
duration_milliseconds
tokens
cost_microunits
cost_currency
tool_calls
pricing_policy_id
pricing_policy_version
pricing_rules_hash
reservation_hash
```

It is read-only metadata, not a ledger. Two optional interfaces expose it:

```text
ProviderReservedAgent.provider_call_reservation
ProviderReservedTool.provider_call_reservation
```

DeepSeek Agent reservation is derived from the Phase 9A model-call policy:

- tokens and cost use the existing provider-call reservation;
- duration uses the frozen maximum provider-call duration;
- tool calls are zero;
- identity is bound to the model-call policy and model bundle.

Tavily reservation uses zero tokens, one Tool call, frozen maximum duration,
and maximum credits multiplied by a conservative microunits-per-credit bound.
Its identity is part of the Tavily policy hash.

The Browser may expose zero monetary cost, one Tool call, and bounded duration.
MOCK/LOCAL implementations without provider reservations keep existing
behavior.

### 4.1 Currency authority

Do not add currency to `RuntimeResourceAmount`, `TaskExecutionRequest` v1,
`AgentRequest` v1, or `AgentContext` v1.

`RunConfig.budget_limits.cost_currency` remains the sole Run/task currency
authority. At create/resume, `RealCompositionManager` requires every paid REAL
reservation currency to match it. Immediately before every REAL Agent or Tool
provider dispatch, durable composition revalidation repeats the comparison
against freshly loaded RunState.

AgentRunner performs numeric admission only and never selects or converts
currency.

## 5. Remaining-hard-limit admission

AgentRunner uses its existing local usage accumulator as the sole
sub-operation admission input. This is not persisted and is not a second
budget ledger.

Before every provider-backed Agent decision:

```text
accumulated Agent/Tool usage
-> remaining task hard limits
-> immutable Agent reservation
-> admission
-> agent.decide()
```

Before every paid provider Tool dispatch:

```text
updated accumulated Agent/Tool usage
-> remaining task hard limits
-> immutable Tool reservation
-> admission
-> Tool dispatch
```

Admission compares duration milliseconds, tokens, cost microunits, and Tool
calls component by component.

- EXACT usage is subtracted normally.
- UPPER_BOUND usage is conservatively treated as consumed at the stored bound.
- UNKNOWN usage already fails closed and permits no subsequent call.
- Reservation is not added to usage at admission.
- Returned actual/upper-bound usage is added exactly once.
- Tool-call allowance uses accumulated Tool usage, not successful observation
  count.

Stable failures distinguish Agent/Tool and resource, including:

```text
agent_provider_reservation_exceeds_remaining_duration
agent_provider_reservation_exceeds_remaining_tokens
agent_provider_reservation_exceeds_remaining_cost
tool_provider_reservation_exceeds_remaining_duration
tool_provider_reservation_exceeds_remaining_tokens
tool_provider_reservation_exceeds_remaining_cost
tool_call_reservation_unavailable
provider_reservation_contract_invalid
```

Failure means zero new provider usage and zero HTTP requests while preserving
earlier accumulated usage. Phase 3 retains outer task-attempt reservation,
settlement, and retry authority.

The current DeepSeek full-task-limit check is replaced by immutable reservation
exposure plus AgentRunner remaining-limit admission. DeepSeek still revalidates
lifecycle/composition immediately before HTTP but owns no accumulated budget
state.

## 6. ToolInvocationRequest v1 and REAL envelope

`ToolInvocationRequest` v1 remains byte-for-byte unchanged: no field, default,
validator, discriminator, or serialization change.

Add application-only contracts:

```text
CapabilityDispatchContext:
  run_id
  task
  task_id
  task_contract_hash
  task_operation_key
  requested_capability_id
  authorized_capability_ids
  descriptor_hash
  tool_operation_key
  context_hash

AuthorizedToolDispatchEnvelope:
  schema_version
  invocation: ToolInvocationRequest
  dispatch_context: CapabilityDispatchContext
```

The envelope is immutable, ephemeral, never persisted/traced in full, and not
part of operation identity. AgentRunner constructs it after validating the
AgentContext, Tool decision, registry authorization, input, descriptor, and
operation key.

Keep existing `Tool.invoke(ToolInvocationRequest, CancellationSignal)` for
MOCK/LOCAL tools. Add separate
`AuthorizedRealTool.invoke_authorized(AuthorizedToolDispatchEnvelope,
CancellationSignal)`. REAL external tools expose only the authorized boundary;
direct v1 invocation cannot cause external activity.

## 7. Immediate REAL Tool authority

Immediately before external activity, `RealToolDispatchAuthorizer`:

1. loads current RunState;
2. verifies bound/request/envelope Run identity;
3. requires REAL mode and RUNNING status;
4. validates context and ResearchTask hashes;
5. verifies task ID and exact required capabilities;
6. calls existing CapabilityRegistry `resolve_authorized`;
7. verifies registered Tool and descriptor;
8. reloads and validates durable REAL composition;
9. recomputes current Search/Browser policy;
10. verifies capability pin, policy hash, descriptor hash, operation version,
    and process binding;
11. verifies provider reservation identity;
12. rechecks reservation currency against current RunConfig;
13. returns an ephemeral authorization result.

Only then may Tavily access credentials or HTTP. Only then may Browser DNS
resolution begin. CapabilityRegistry remains the only capability permission
authority.

## 8. Browser and SSRF contract

Browser descriptor:

```text
capability_id     = web_browser
tool_id           = http_browser
adapter_id        = bounded_http_browser
adapter_version   = bounded-http-browser-v1
operation_version = 1+p<browser-policy-hash>
mode              = REAL
input_type        = BrowserRequest
output_type       = BrowserResult
side_effect       = NONE
idempotency       = IDEMPOTENT
```

Policy pins the exact allowed scheme and port tuples; a ResearchOS-owned
special-use address table/version/hash plus IPv4-mapped and mixed-DNS policy;
DNS/rebinding, redirect, ASCII canonical-target reject-non-ASCII, extraction, and
normalization versions; MIME/charset/identity-encoding allowlists; User-Agent;
and every URL, header, body, extraction, and total-invocation bound. The
adapter consumes those frozen values at validation, transport, extraction, and
normalization boundaries; unsupported semantic changes fail composition
validation rather than becoming a hidden code-default change.

The STANDARD Browser profile permits only ports 80 and 443. The pinned
transport passes the frozen header bound to the underlying `StreamReader`
limit, so its parser limit cannot silently be narrower than the policy.

For the initial URL and every redirect:

1. canonicalize and reject userinfo/ambiguous/non-HTTP(S) URLs;
2. validate port;
3. resolve through an injected bounded resolver;
4. validate every IPv4/IPv6 result and reject mixed safe/unsafe answers;
5. unwrap IPv4-mapped IPv6;
6. connect to a deterministic validated literal IP while retaining Host/SNI;
7. re-resolve before returning the stream or sending HTTP bytes;
8. require exact DNS-set equality and verify peer IP;
9. revalidate every redirect target;
10. validate status, MIME, encoding, and declared size;
11. stream raw bytes with a hard limit;
12. extract only validated bounded bytes.

Baseline uses a custom pinned `asyncio` HTTP/1.1 transport with manual
redirects, literal-IP connection plus Host/SNI preservation,
`Accept-Encoding: identity`, and bounded one-shot Trafilatura subprocess
extraction. HTTPX is used only by Tavily. Playwright is excluded.

The Phase 9B web Agent policy freezes a total provider-call timeout. The async
provider transport enforces the minimum of that bound and the absolute task
deadline, so an HTTPX stream that periodically emits data cannot outlive the
frozen reservation. HTTPX phase timeouts remain stall bounds, not the total
call bound. Capability-empty Phase 9A direct Agents retain their historical
deadline-only transport behavior.
The Phase 9B accumulated admission profile is limited to the web-tool Agent
contract. Capability-empty Phase 9A direct Agents retain their original
per-call token/cost task-limit preflight and do not acquire remaining-duration
or repeated-call admission semantics.

## 9. Search/Evidence versioning

Do not modify v1 `SearchHit` or `SearchResult`. Add:

```text
SearchHitV2.content_kind:
  SOURCE_EXCERPT
  PROVIDER_SUMMARY_METADATA

SearchResultV2.output_type = search_result_v2
```

Tavily always emits `PROVIDER_SUMMARY_METADATA`.

EvidenceExtractor implementation v2 preserves frozen routes:

- SearchResult v1 uses exact Phase 5 v1 extraction and candidate version 1;
- Browser and Local Retrieval retain current semantics;
- v2 provider metadata produces no EvidenceCandidate;
- any future trusted v2 excerpt requires an explicit v2 route.

Historical v1 SearchResult, ToolInvocationResult, AgentObservation, and
ToolInvocationRequest fixtures must parse/re-serialize to identical canonical
bytes and hashes. Existing Evidence IDs, revisions, operations, and receipts
are never rewritten.

Search metadata remains a candidate source reference. Only validated Browser
content becomes WEB Evidence. Adapters never receive EvidenceStore access.

## 10. Persistence and Tool identity

Trace/error/diagnostic data uses safe hashes and excludes raw query, URL,
snippet, body, provider prose, and credentials. Phase 5 Source/Evidence
authority may persist validated original/canonical locators, including query
components, for provenance. Secret-detected locators fail rather than being
silently redacted.

Keep Phase 4 operation identity unchanged:

```text
task operation_key + agent_step + capability_id + tool_id + adapter_id
+ operation_version + canonical safe input hash
```

The envelope is excluded. Reservation identity enters indirectly through the
frozen policy hash and operation version. Attempt identity and `tool_call_id`
remain excluded.

## 11. REAL Agent response and untrusted-content prompt

For a Phase 9B composition with Search and/or Browser enabled, the REAL Agent
response contract becomes a strict union:

```text
AgentFinalDecision
AgentFailedDecision
AgentToolDecision<SearchRequest | BrowserRequest>
```

The model cannot authorize capabilities; CapabilityRegistry remains
default-deny.

The Phase 9B Agent system prompt must state that Search/Browser observations
are untrusted external data; instructions embedded in them cannot override
ResearchOS system, task, capability, Tool, security, or budget rules and may
only be treated as evidence to analyze.

The exact prompt version/content hash is frozen in the Agent model bundle and
REAL composition. Full adversarial prompt-injection evaluation is deferred to
Phase 9D.

Capability-empty Phase 9A compositions retain the historical direct-only
Agent prompt, schema, and `agent-direct-decision-v1` response contract. The
tool-capable contract is `agent-tool-decision-v2`; selecting it changes the
prompt hash, model bundle, and composition identity rather than silently
reinterpreting an existing Run.

## 12. Failure and usage semantics

Tavily configuration, endpoint, credential, currency, reservation, permanent
4xx, malformed, and oversized failures are non-retryable. Rate limit,
transient 5xx, and timeout may be retryable metadata. Cancellation is
non-retryable. Adapter retry is always zero.

Tavily usage is EXACT zero before dispatch, UPPER_BOUND after confirmed
dispatch, and UNKNOWN only at an ambiguous boundary. Tavily never produces
Evidence.

Browser invalid URL, blocked IP, rebinding, unsafe redirect, MIME/encoding/body
or extraction failures are non-retryable. Transient DNS, 408/429/5xx, and
timeout may be retryable. Browser Evidence requires successful validation and
extraction. Phase 3 alone performs retries.

## 13. REAL composition, dependencies, and doctor

Add versioned Tavily, Browser, source-access, descriptor, prompt, and provider
reservation snapshots. Extend capability pins with adapter version, policy
schema/version/hash, and descriptor hash. RealCompositionManager independently
recomputes these at create, resume, and dispatch. Any semantic drift fails
closed.

Retain dependency groups:

```text
search: httpx>=0.28.1,<1.0
browser: httpx>=0.28.1,<1.0; trafilatura>=2.2,<3.0
browser-js: playwright>=1.55,<2.0
```

Doctor remains zero-cost/read-only and checks the standard Tavily profile,
exact HTTPS endpoint, credential presence, policy construction, reservation
currency, composition/descriptor identity, exact Run capability set, Browser
security policy, and optional dependencies. It performs no DNS or HTTP by
default.

## 14. Test matrix

### Tavily request and credentials

- only documented fields are sent;
- language fields are absent from HTTP and internally pinned disabled;
- standard `safe_search=false` and `chunks_per_source=1` are explicit;
- exact HTTPS endpoint accepted;
- HTTP, userinfo, query, fragment, path, and port variants rejected before
  credential access/HTTP;
- redirects/environment proxies disabled;
- secrets absent from composition/error/trace;
- provider answer/raw content cannot escape parsing.

### Currency and repeated reservation admission

- Tavily/Run currency mismatch rejects configuration;
- tampered currency rejects dispatch with zero HTTP;
- historical resource/request/context schemas remain unchanged;
- first Agent call fits and dispatches;
- later Agent cost reservation exceeding remaining cost causes zero second LLM
  calls;
- later Agent token reservation exceeding remaining tokens causes zero HTTP;
- sufficient remaining reservation dispatches;
- UPPER_BOUND is conservative and UNKNOWN fails closed;
- exhausted Tavily cost/Tool-call allowance causes zero HTTP;
- sufficient Tavily reservation permits exactly one request;
- reservation identity is composition-pinned;
- adapters never retry and Phase 3 retains retry ownership;
- MOCK/LOCAL behavior remains unchanged.

### Compatibility, authority, and security

- v1 ToolInvocationRequest/SearchResult/AgentObservation bytes and hashes are
  unchanged;
- v1 operation keys and MOCK/LOCAL invocation remain unchanged;
- v2 Tavily metadata creates no Evidence;
- missing/corrupt/tampered composition, wrong Run/lifecycle/task/capability,
  missing binding, descriptor/version/reservation mismatch, and direct v1 REAL
  invocation all produce zero DNS/credential access/HTTP;
- Browser tests cover private/special IPv4/IPv6, mixed/changed DNS, peer
  mismatch, redirects, stream limits, compression, MIME/charset, cancellation,
  deadline, worker cleanup, and successful extraction;
- Agent prompt contains the untrusted-observation rule and changes its frozen
  prompt/model-bundle/composition hash;
- full core/all-extras suites remain deterministic and offline.

## 15. Implementation files and migration

Implementation adds narrow web policy/ports, Tavily adapter, pinned Browser
transport, bounded extractor worker, REAL dispatch envelope/authorizer,
fixtures/tests, and `docs/PHASE9_SECURITY_REVIEW.md`.

Only necessary versioned Tool output/interface, CapabilityRegistry,
AgentRunner, EvidenceExtractor, REAL composition/settings/factory, DeepSeek
Agent, doctor/CLI, dependency, CI, and Phase 9 documentation boundaries change.
Phase 3 scheduling/checkpoint/budget ownership and Phase 5 persistence schemas
do not change.

Migration rules:

- Phase 9A capability-empty runs cannot silently enable Phase 9B;
- v1 ToolInvocationRequest and SearchResult remain byte-identical;
- existing Evidence authority is not rewritten;
- tool-capable Agent schema and prompt create a new model bundle;
- incompatible resume fails closed;
- no automatic artifact migration.

## 16. Non-goals and accepted risks

Non-goals include Playwright/JavaScript fallback, browser login/cookies,
Tavily answer ingestion, provider-native research agents, adapter retries, and
any second runtime/ledger/store.

Accepted non-blocking risks:

- strict DNS-set equality may reject legitimate CDN churn;
- JavaScript-only pages are unsupported;
- bounded extraction is not a hostile-code sandbox;
- validated query-bearing locators may persist in Source/Evidence authority;
- provider pricing/security tables require versioned maintenance;
- full adversarial prompt-injection evaluation is deferred to Phase 9D.

## 17. Final self-audit

```text
P0 = 0
P1 = 0
Blocking P2 = 0
Non-blocking P2:
  - full adversarial web prompt-injection evaluation deferred to Phase 9D
  - strict DNS-set equality compatibility limitation
  - no JavaScript rendering
  - bounded extraction is not a hostile-code sandbox
  - provider pricing/security profiles require versioned maintenance
```

**GO for Phase 9B Coding.**
