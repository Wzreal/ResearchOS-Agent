"""Trusted extraction of eligible successful Agent observations."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from researchos.domain.agent import AgentContext, AgentObservation
from researchos.domain.evidence import EvidenceCandidate, SourceType
from researchos.domain.identity import (
    normalize_content,
    sha256_text,
    stable_hash,
    stable_id,
)
from researchos.domain.tools import (
    BrowserResult,
    LocalRetrievalResult,
    SearchResult,
    ToolInvocationStatus,
)


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = (
        host
        if port is None
        or (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)}
        else f"{host}:{port}"
    )
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


class EvidenceExtractor:
    extractor_id = "phase5_extractor"
    extractor_version = "1"

    def extract(
        self, context: AgentContext, observation: AgentObservation
    ) -> tuple[EvidenceCandidate, ...]:
        result = observation.result
        if result.status is not ToolInvocationStatus.SUCCEEDED:
            return ()
        output = result.output
        common = {
            "run_id": context.run_id,
            "run_revision": context.run_revision,
            "task_id": context.task_id,
            "task_operation_key": context.task_operation_key,
            "attempt_id": context.attempt_id,
            "attempt_number": context.attempt_number,
            "agent_step": observation.agent_step,
            "tool_call_id": observation.tool_call_id,
            "tool_input_hash": observation.tool_input_hash,
            "tool_operation_key": observation.tool_operation_key,
            "adapter_id": observation.adapter_id,
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
        }
        if isinstance(output, BrowserResult):
            if output.content_hash != sha256_text(output.content):
                return ()
            locator = canonicalize_url(output.final_url)
            return (
                EvidenceCandidate(
                    **common,
                    source_type=SourceType.WEB,
                    original_locator=output.final_url,
                    canonical_locator=locator,
                    evidence_scope_key="browser-page-body-v1",
                    media_type="text/plain",
                    content=output.content,
                    observed_at=output.retrieved_at,
                    extraction_context={"output_type": output.output_type},
                ),
            )
        if isinstance(output, LocalRetrievalResult):
            return tuple(
                EvidenceCandidate(
                    **common,
                    source_type=SourceType.LOCAL_DOCUMENT,
                    original_locator=hit.locator,
                    canonical_locator=f"local://{hit.document_id}",
                    evidence_scope_key=stable_id(
                        "scope", ["local-chunk-v1", hit.document_id, hit.chunk_id]
                    ),
                    media_type="text/plain",
                    content=hit.content,
                    observed_at=output.retrieved_at,
                    extraction_context={
                        "document_id": hit.document_id,
                        "chunk_id": hit.chunk_id,
                    },
                )
                for hit in output.hits
                if hit.content_hash == sha256_text(hit.content)
            )
        if isinstance(output, SearchResult):
            candidates = []
            for hit in output.hits:
                locator = canonicalize_url(hit.url)
                snippet_identity = stable_hash(
                    [locator, sha256_text(normalize_content(hit.snippet))]
                )
                candidates.append(
                    EvidenceCandidate(
                        **common,
                        source_type=SourceType.WEB,
                        original_locator=hit.url,
                        canonical_locator=locator,
                        evidence_scope_key=stable_id(
                            "scope",
                            [
                                "search-snippet-v1",
                                observation.tool_input_hash,
                                snippet_identity,
                            ],
                        ),
                        media_type="text/plain",
                        content=hit.snippet,
                        observed_at=hit.retrieved_at,
                        extraction_context={
                            "output_type": "search_result",
                            "snippet_locator_hash": sha256_text(snippet_identity),
                        },
                    )
                )
            return tuple(candidates)
        return ()
