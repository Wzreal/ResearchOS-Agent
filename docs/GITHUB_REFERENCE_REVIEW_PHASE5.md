# GitHub Reference Review — Phase 5

Review date: 2026-08-28. Scope: Evidence Memory, provenance, claim/evidence
relations, and citation integrity only. Reference code informed boundary checks;
no module or large source block was copied.

## KEEP

- Keep ResearchOS provider-independent typed observations and immutable local
  provenance. `browser-use` exposes structured action results, visited URLs,
  extracted content, errors, and typed structured output; this supports taking
  only a small validated result surface rather than browser state, screenshots,
  DOM, or model history into Evidence Memory.
- Keep explicit web/local source types. GPT Researcher exposes distinct web,
  local, and hybrid sources, while Deep Searcher separates retrieval references
  from the answer-generation layer. ResearchOS retains the source type and
  canonical locator instead of flattening all content into prompt text.
- Keep stable URL/source identity and downstream citation references. Open Deep
  Research assigns one citation per unique URL, and Alibaba WebWeaver retains
  URL-to-summary and URL-to-page/evidence mappings. ResearchOS strengthens this
  pattern with immutable evidence revisions and edge references.
- Keep structural citation validation separate from model judgement. The
  references show citations as report/research artifacts; they do not justify
  treating an LLM verdict as integrity. Phase 5 therefore verifies identity,
  hashes, revisions, and references only.

## ADJUST

- Preserve query-dependent Search snippets independently from page bodies.
  URL-only citation numbering is useful for final presentation but is too
  coarse for storage: different queries may yield different snippets for one
  URL. ResearchOS includes safe query context and stable snippet identity in
  the Search scope while Browser bodies remain page-scoped.
- Preserve successful observations even when the enclosing Agent later fails.
  Browser Use history distinguishes action results from the final success flag.
  ResearchOS consequently ingests validated successful observations before it
  maps final Agent status, without importing the entire Agent history.
- Represent stale sources as addressable historical revisions rather than
  silently replacing them. GPT Researcher's benchmark notes that stale archived
  document vintages can produce incorrect conclusions. ResearchOS reports valid
  superseded citations as warnings and leaves truth/quality assessment to later
  verification.
- Fail closed when no eligible evidence exists. An open GPT Researcher issue
  describes fabricated sources after empty retrieval context. ResearchOS never
  creates evidence, claims, or citations from an empty/failed Tool result.

## REJECT

- Reject prompt text or a final report's numbered URL list as the canonical
  evidence database. Those formats are presentation-oriented and do not encode
  immutable content revisions, runtime occurrence receipts, or integrity
  hashes.
- Reject copying complete browser histories, screenshots, model thoughts, or
  raw provider payloads into Evidence Memory. They exceed the trusted boundary,
  increase secret exposure, and make idempotency depend on volatile runtime
  state.
- Reject adopting Milvus/vector-database identity or retrieval scores as
  evidence identity. Deep Searcher's storage choices are useful for future
  retrieval scale, but Phase 5 needs deterministic local contracts and must not
  commit ResearchOS to one database or embedding provider.
- Reject importing Alibaba training/evaluation pipelines, Open Deep Research
  report synthesis, or GPT Researcher report generation. They belong to later
  ResearchOS phases and would collapse evidence capture, synthesis, and truth
  assessment into one boundary.

## Minimal resulting adjustments

The review validates the approved design. The only implementation-level
adjustments are narrow: Search snippet scope includes safe query context;
successful observations survive an enclosing Agent failure; empty/failed
results produce no evidence; and citation staleness is a warning distinct from
structural corruption. No reference-driven architecture rewrite was made.

## References

- <https://github.com/langchain-ai/open_deep_research>
- <https://github.com/assafelovic/gpt-researcher>
- <https://github.com/browser-use/browser-use>
- <https://github.com/Alibaba-NLP/DeepResearch>
- <https://github.com/zilliztech/deep-searcher>
