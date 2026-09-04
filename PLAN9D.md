# Phase 9D — Optional Observability Backend

Phase 9D mirrors the existing canonical local trace to optional OTLP/HTTP
protobuf export. The local `TraceSink` remains the authority; export is
best-effort and creates no budget, retry, checkpoint, evidence, or lifecycle
semantics.

## Frozen implementation constraints

- `ObservabilityRuntime` owns exporter workers only when enabled. It accepts
  synchronous `TraceSink` callers through a bounded thread-safe queue; local
  append completes first and `offer` performs no network I/O. Disabled mode
  creates no loop or thread. Shutdown is explicit and may drop non-durable
  remote work.
- `append_once(APPENDED)` offers one envelope; `ALREADY_PRESENT` offers none;
  corruption propagates. There is no remote replay, remote queue, or retry.
- The single `project_trace_event` boundary maps event type to scope and reads
  identities solely from fixed schema fields and allowlisted attribute keys.
  Local-only observability diagnostics are never exported.
- OTLP is direct HTTPS only, never follows redirects or environment proxies,
  and bounds connection/read/write/pool time plus request/response bytes.
  Configured auth header names are RFC-token validated and structural or
  hop-by-hop headers are rejected. Header values come only from a secret slot,
  reject controls/CRLF, and never enter settings, traces, diagnostics, errors,
  or OTLP attributes.

## Non-goals

No vendor SDK, durable remote delivery, security/release gate, real E2E,
operator runbook, or changes to Phase 1–9C authorities.
