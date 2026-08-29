# Phase 6 Design — Synthesis and Bounded Verification

Phase 6 consumes immutable Phase 5 Claim Graph and Evidence Memory snapshots.
It produces a claim-addressable report draft, bounded Red/Blue/Judge review,
an authoritative verification artifact, and deterministic Markdown. It does
not decide factual truth.

## Semantic locks

1. Stable identities exist for verification, synthesis, citation, finding, and
   acquisition request. They exclude model prose, rationale, round, and
   attempt identity.
2. Replay order is fixed: freeze snapshots, compute snapshot hashes, compute
   verification identity, then look up authority. Same-identity replay makes
   zero model calls.
3. Publication renders and hashes Markdown first, writes `verification.json`
   second, and writes those already-rendered bytes to `report.md` last.
4. Claim and Evidence context admission is whole-item only. Omitted item IDs
   are explicit; content is never truncated under its original identity/hash.
5. Report claims remain stable entities with `INCLUDED` or `REMOVED` state.
   Publishable means INCLUDED plus a SUPPORTED or QUALIFIED Judge verdict, no
   unresolved blocking finding, and no citation-integrity ERROR. SUPPORTED
   requires STRUCTURALLY_SUPPORTED. REMOVE clears section membership and
   citation assignments without deleting the entity.
6. Every Red finding pins a report claim. Contradictory-evidence findings pin
   evidence; citation gaps have no citation pin. Any optional pin must be in
   the frozen invocation.
7. `derive_current_valid_edges` is the sole edge-admission rule. An edge is
   usable only when the edge, Claim, and Evidence are ACTIVE, the edge revision
   is current, and both revision pins equal the entities' current revisions.
   Stale pins are never upgraded; dangling cross-store pins are typed input
   corruption.
8. Blue actions bind exactly one open finding and cannot dispose findings.
   Judge decides every ReportClaim and dispositions every open finding exactly
   once. A stable finding ID is cumulative-quota identity, not permanent
   resolution; a later Red emission reopens it.
9. Every persisted citation is a `CitationAssignment` naming its current-valid
   edge and relation. SUPPORTED and publishable QUALIFIED Claims require a
   SUPPORTS citation. A conflicted Claim published as QUALIFIED also retains a
   CONTRADICTS citation; CONTEXTUALIZES never establishes support.
10. Draft identity hashes synthesis identity, revision number, parent revision
    ID, and canonical content hash. Model-authored prose and titles are escaped
    as plain text; only the deterministic renderer owns Markdown headings,
    citation markers, and footnote definitions.

## Boundaries and flow

`VerificationService` requires a loaded Run already in `VERIFYING`, freezes the
two Phase 5 stores, performs replay lookup, and invokes one
`VerificationCoordinator`. The coordinator calls a provider-independent async
model port in the order Synthesizer, then bounded Red/Blue/Judge rounds. Judge
returns FINALIZE or CONTINUE; continuation must match an open finding or frozen
structural conflict, and total calls cannot exceed `1 + 3 * max_rounds`. Mock
fixtures are selected exactly by verification ID, role, round, and draft
revision. Missing fixtures fail explicitly and never fall back.

The exact `verification_id` input is run ID, run revision, Claim snapshot hash,
Evidence snapshot hash, policy hash, and model-bundle hash. Synthesis uses the
stable `initial` discriminator. Report Claim and citation identities use only
immutable entity/revision/source/content pins.

Blue may KEEP, QUALIFY, REMOVE, ADD_EXISTING_CITATION, or REQUEST_EVIDENCE. It
returns exactly one action per open finding; multiple findings may address one
Claim, with deterministic conflict checks for incompatible mutations. A
request cannot affect the current frozen invocation. Judge pins the final draft
revision, returns exact Claim decisions and finding dispositions, and cannot
author prose or citations. Structural support comes only from current-valid
frozen SUPPORTS/CONTRADICTS relations; contextual evidence is not support.

The completed disposition is VERIFIED, PARTIALLY_VERIFIED, REJECTED, or
INCONCLUSIVE. Operational failure, cancellation, and deadline expiry are typed
failures and are never disguised as INCONCLUSIVE.

## Persistence and ownership

`outputs/<run_id>/verification.json` is authoritative and `report.md` is a
derived, reconcilable projection. Both reject data that would require
redaction. Immediately before publication, a revision/hash-only fingerprint
boundary proves the two live stores still equal the frozen input; otherwise
publication fails with `verification_input_changed`. Phase 6 has no round
checkpoint, WAL, durable retry, scheduler, or
budget ledger. It returns actual local usage and `UsageCertainty`; Phase 3
retains durable execution and settlement ownership.

The authority contains both store revisions, selected/omitted IDs, complete
draft lineage, validated round records, acquisition requests, citation issues,
final disposition and termination reason, usage certainty, Markdown hash, and
supersession identity. A canonical top-level content hash detects silent field
tampering. Same-verification-ID publication compares the complete canonical
artifact before touching `report.md`. Persistence errors separately expose
authority, report, and overall artifact commit state; a completion-trace failure
after publication is typed with `artifact_committed=True`.

Coordinator usage, certainty, expected mode, and event callback are invocation
local, so one coordinator is reentrant across concurrent runs. UNKNOWN usage
after a Judge FINALIZE may be published honestly; UNKNOWN plus CONTINUE stops
before another Red call. Termination distinguishes `JUDGE_FINALIZED`,
`MAX_ROUNDS`, and `MAX_FINDINGS`. The derived report visibly states its
disposition and discloses conflicts, omitted/unresolved Claims, and pending
acquisition rather than presenting partial work as cleanly verified.
