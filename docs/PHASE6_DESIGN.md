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

Blue may KEEP, QUALIFY, REMOVE, ADD_EXISTING_CITATION, or REQUEST_EVIDENCE. A
request cannot affect the current frozen invocation. Judge pins the final draft
revision and cannot author prose or citations. Structural support comes only
from current frozen SUPPORTS/CONTRADICTS relations; contextual evidence is not
support.

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
