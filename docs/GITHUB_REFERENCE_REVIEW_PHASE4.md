# Phase 4 GitHub Reference Review

**Review date:** 2026-08-28  
**Scope:** Implemented Phase 4 Agent/Tool boundaries only. This is a selective
architecture comparison, not a feature-parity exercise and not an endorsement
of copying reference implementations.

## Sources reviewed

- [langchain-ai/open_deep_research](https://github.com/langchain-ai/open_deep_research)
  and its
  [main researcher graph](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py)
- [assafelovic/gpt-researcher](https://github.com/assafelovic/gpt-researcher)
- [browser-use/browser-use](https://github.com/browser-use/browser-use), its
  [Agent service](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py),
  and documented
  [tool result model](https://github.com/browser-use/browser-use/blob/main/AGENTS.md)
- [Alibaba-NLP/DeepResearch](https://github.com/Alibaba-NLP/DeepResearch),
  including its
  [ReAct inference loop](https://github.com/Alibaba-NLP/DeepResearch/blob/main/inference/react_agent.py)
  and [WebWatcher tool set](https://github.com/Alibaba-NLP/DeepResearch/blob/main/WebAgent/WebWatcher/README.md)
- [zilliztech/deep-searcher](https://github.com/zilliztech/deep-searcher),
  including
  [DeepSearch](https://github.com/zilliztech/deep-searcher/blob/master/deepsearcher/agent/deep_search.py)
  and the
  [vector database boundary](https://github.com/zilliztech/deep-searcher/blob/master/deepsearcher/vector_db/base.py)

The review used repository-owned README, documentation, and source snapshots.
Reference behavior may evolve after the review date.

## KEEP

| ResearchOS decision | Reference evidence | Finding |
|---|---|---|
| Provider-independent Agent and Tool ports | Open Deep Research configures multiple model/search providers and MCP tools; GPT Researcher supports configurable research sources; Deep Searcher separates LLM, embedding, and vector database bases. | Keep the narrow domain-facing protocols. They provide the useful extensibility without importing a framework or vendor into the runtime. |
| Application-owned bounded Agent loop | Browser Use applies a step timeout and a maximum step count; Alibaba DeepResearch also bounds available model calls. | Keep `AgentRunner` ownership of step count, attempt deadline, and cancellation. ResearchOS is stricter because each Agent decision and Tool await is independently bounded. |
| Structured decision, observation, and result contracts | Browser Use exposes structured action results with content, errors, completion, success, and attachments. Alibaba DeepResearch records tool interactions in a message trajectory. | Keep typed `AgentDecision`, `AgentObservation`, `ToolInvocationResult`, usage certainty, errors, and artifacts instead of raw strings. |
| Explicit capability authorization | Browser Use supports an explicit Tool registry and action exclusion; the other projects select configured tool sets. | Keep registry lookup separate from permission. Exact membership in the task-required capability set and default deny are stronger and fit ResearchOS's validated DAG boundary. |
| Stable logical Tool identity independent of model call IDs | Reference projects commonly retain model/tool-call trajectory IDs but do not provide ResearchOS's durable retry identity contract. | Keep the Phase 3 operation key plus Agent step, capability, Tool/adapter/version, and safe input hash. Do not weaken it to a model-provided call ID. |
| Retrieval provenance and deterministic ordering | GPT Researcher emphasizes source tracking; Deep Searcher returns retrieval records and deduplicates retrieved results. | Keep locator, adapter, retrieval time, source metadata, content hash, score, deterministic BM25 ordering, and stable tie breaking. |
| Browser/Search as contracts before real providers | Open Deep Research and GPT Researcher demonstrate that provider selection changes over time and spans many search backends. | Keep Browser/Search provider-independent and keep real integrations gated until configuration, credentials, timeout, and contract tests exist. |
| Explicit Python subprocess limits with an honest threat model | Alibaba DeepResearch exposes a code-interpreter capability, while browser/research projects generally rely on external runtime assumptions. | Keep the bounded local subprocess for trusted code only, declared artifacts, output bounds, sanitized environment, disabled stdin, and cancellation cleanup. Keep stating that it is not a hostile-code sandbox. |

## ADJUST

These were concrete Phase 4 gaps found during the review and coverage audit;
the implementation changes are deliberately local.

| Gap | Minimal adjustment |
|---|---|
| A scripted/mock adapter could carry a `REAL` descriptor and therefore misrepresent fixture behavior when a composition enabled real mode. | `ScriptedAgent` and `MockTool` now reject any descriptor whose mode is not `MOCK`. Registry mode validation remains a separate composition check. |
| Python inherited the parent stdin, allowing an unintended interactive wait outside the supported execution contract. | Launch the subprocess with `stdin=DEVNULL`; retain argument-vector execution and never use `shell=True`. |
| Python limited each artifact's size but did not limit the number of declared artifacts. | Add `max_artifact_count` to `PythonSubprocessPolicy` and fail the invocation before publication when exceeded. |
| Phase 3 cancellation could race a scripted Agent exception while `AgentRunner` was cleaning up child awaits. | Suppress child-task exceptions only in the already-cancelled cleanup path, then re-raise the controlling cancellation. This prevents unobserved child failures without hiding normal execution errors. |
| Provenance dictionaries were typed but did not explicitly reject non-finite/non-JSON data at the public contract boundary. | Add finite-JSON validation to local retrieval, search, and browser provenance fields. |

## REJECT

| Reference pattern | Reason for rejection in ResearchOS Phase 4 |
|---|---|
| Transplanting Open Deep Research's LangGraph state, commands, supervisor graph, or provider initialization | It would duplicate the Phase 2/3 planner and durable scheduler, couple the domain to a framework, and change recovery semantics. |
| Adopting GPT Researcher's complete planner/executor/publisher workflow or implicit provider fallback behavior | ResearchOS already has validated DAG and lifecycle ownership. Phase 4 must not become a second orchestration layer or silently substitute providers. |
| Adopting Browser Use's browser session state machine, large default action registry, or general computer-control surface | A browser engine and general computer control are explicit non-goals. Phase 4 needs only a typed capability boundary; the real browser adapter is Phase 9. |
| Adopting Alibaba DeepResearch's global `TOOL_MAP`, name-based dispatch branches, stringified errors/results, or synchronous `asyncio.run` calls inside Tool routing | These patterns blur authorization, identity, async cancellation, and structured failure contracts. They would weaken the current boundary. |
| Coupling local retrieval to Deep Searcher's Milvus/vector database, embedding model, or iterative LLM query expansion | Phase 4 requires deterministic offline local retrieval. Vector/embedding providers and query expansion would add external systems and planning behavior outside scope. |
| Claiming the Python import allowlist is a security sandbox | AST filtering is a supported-code policy only. Hostile-code isolation requires a container/VM and remains Phase 9 work. |
| Adding a generic durable Tool journal, Agent checkpoint, or Tool retry loop to imitate reference agent histories | Phase 3 owns durable attempts, retry, checkpointing, and budget settlement. Phase 4 preserves stable identity and reports trace failures but does not create a second durability system. |

## Conclusion

The references validate the broad direction—configurable providers, bounded
agent steps, typed tool outcomes, explicit tool sets, and source tracking—but
do not justify a ResearchOS architectural rewrite. The only accepted changes
were the five narrow hardening items above. Evidence/claim ingestion,
synthesis, real browser/search/LLM providers, and hostile-code isolation remain
outside Phase 4.
