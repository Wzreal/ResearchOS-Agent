# Phase 9B Search and Browser Security Review

## Trust boundaries

Model-authored Tool calls are untrusted requests. Registry existence is not
permission. Before Tavily can read a credential or Browser can perform DNS, the
REAL Tool authorizer verifies the current Run is REAL/RUNNING, the complete
ResearchTask contract and hash, required capability membership, registry
binding, immutable composition, descriptor/operation/policy identity,
reservation identity, and budget currency. Raw provider exceptions, secrets,
provider request IDs, response bodies, and environment values are not written
to semantic traces.

## Tavily

Phase 9B supports only `POST https://api.tavily.com/search` with the frozen
STANDARD feature profile. The adapter sends an explicit documented field set;
language filtering is disabled and absent, `safe_search=false` is explicit,
and raw content/answer/image outputs are disabled. A request-scoped HTTPX
client has redirects and environment proxies disabled and the adapter performs
one call with no retry; it is closed before the Tool invocation returns.
A conservative versioned one-credit USD upper-bound profile controls admission;
unknown profiles, endpoint identity drift, underpriced rates, currency drift,
or request/response bound violations fail closed.

Tavily snippets are provider-generated summary metadata. They may be observed
by the Agent but `EvidenceExtractor` deterministically creates zero Evidence
from them. Only source excerpts explicitly typed as such can use the v2 search
evidence route; Browser page bodies remain the normal web-evidence source.

## Bounded HTTP Browser

The Browser policy freezes exact schemes/ports, a ResearchOS-owned special-use
network table plus canonical table hash, IPv4-mapped and mixed-DNS rules,
DNS/rebinding and redirect versions, ASCII canonical-target reject-non-ASCII and
text-normalization versions, MIME/charset/identity-encoding policy,
extraction version, User-Agent, and every relevant bound. The Browser accepts
only canonical ASCII absolute HTTP(S) URLs without userinfo, whitespace,
control characters, fragments, invalid percent escapes, Unicode ambiguity, or
nonstandard ports. Each initial or redirected hop:

1. validates URL syntax and policy;
2. resolves a bounded address set and rejects any non-public/special address;
3. resolves again immediately before connecting and requires the same set;
4. connects to a selected numeric address;
5. verifies the connected peer address;
6. preserves the original hostname for HTTP Host and TLS SNI/certificate checks.

Redirect following is disabled in the custom pinned asyncio HTTP/1.1
transport. The Tool handles a bounded redirect count and reapplies the complete
hop policy. The frozen total invocation timeout bounds DNS, every hop, and
extraction, and equals the provider reservation duration. Requests advertise
only identity encoding. Response headers and streamed bodies are bounded;
ambiguous framing, compression, unsupported MIME, or unsupported charset is
rejected. HTML/plain text extraction runs in a time-bounded subprocess with a
minimal environment and bounded output. The policy is an SSRF and accidental
resource-abuse boundary for this adapter; it is not a general hostile-code or
OS network sandbox.

## Ownership and residual risk

AgentRunner owns local repeated provider reservation admission and maintains
the sole per-attempt usage accumulator. A Tool invocation that starts and
returns a known terminal result consumes one logical tool call even if no
provider HTTP request was dispatched; pre-invocation reservation rejection
consumes zero. Phase 3 owns durable budget settlement,
retry, cancellation, and checkpointing. A retryable provider Tool failure ends
the current Agent attempt and is never retried inside the adapter or Agent
loop. Phase 5 owns Evidence identity and persistence.

This delegation is explicit: only Tavily and Browser mark their infrastructure
failures for Phase 3 retry ownership. Historical MOCK/LOCAL Tools keep their
existing retryable-observation behavior. Capability-empty Phase 9A direct
Agents retain their original token/cost task-limit preflight; Phase 9B
remaining-budget admission applies only to the versioned web-tool Agent
profile. Web Agent provider operations are bounded as whole operations by the
minimum of the frozen total-call timeout and task deadline; HTTPX phase timeouts
remain individual stall bounds.

`RealIntegrationFactory.capability_registry()` releases a cached registry and
rejects access when it observes a terminal Run; explicit eviction remains
available for a serving layer that tears down earlier.

Accepted residual risks include single-process composition binding/CAS,
non-durable remote Tool dedupe, DNS and public-host availability dependence,
provider pricing-profile maintenance, and subprocess isolation that is not a
container/VM security boundary. JavaScript browsing, authenticated browsing,
retrieval providers, and real-provider E2E validation are deferred.
