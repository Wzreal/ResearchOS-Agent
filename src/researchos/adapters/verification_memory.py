"""In-memory authoritative Phase 6 artifact store."""

from __future__ import annotations

import hashlib

from researchos.application.errors import VerificationArtifactConflict
from researchos.application.verification_publisher import render_markdown
from researchos.domain.synthesis import VerificationResult
from researchos.security.redaction import PersistenceRedactor


class InMemoryVerificationArtifactStore:
    def __init__(self) -> None:
        self._results: dict[str, VerificationResult] = {}
        self._reports: dict[str, bytes] = {}
        self._redactor = PersistenceRedactor()

    def load(self, run_id: str) -> VerificationResult | None:
        result = self._results.get(run_id)
        if result is None:
            return None
        expected = render_markdown(
            result.final_draft,
            result.judge_decisions,
            findings=result.findings,
            resolved_finding_ids=result.resolved_finding_ids,
            citation_issues=result.citation_issues,
        )
        if hashlib.sha256(expected).hexdigest() != result.markdown_sha256:
            raise VerificationArtifactConflict("authoritative Markdown hash differs")
        return result.model_copy(deep=True)

    def publish(
        self,
        result: VerificationResult,
        markdown: bytes,
        *,
        expected_prior_verification_id: str | None,
    ) -> None:
        self._redactor.assert_safe_model(result)
        if hashlib.sha256(markdown).hexdigest() != result.markdown_sha256:
            raise VerificationArtifactConflict("Markdown hash differs")
        current = self._results.get(result.run_id)
        if current is not None and current.verification_id == result.verification_id:
            self._reports[result.run_id] = markdown
            return
        if (
            None if current is None else current.verification_id
        ) != expected_prior_verification_id:
            raise VerificationArtifactConflict("authoritative artifact CAS conflict")
        self._results[result.run_id] = result.model_copy(deep=True)
        self._reports[result.run_id] = markdown

    def reconcile_report(self, run_id: str) -> bool:
        result = self._results.get(run_id)
        if result is None:
            return False
        expected = render_markdown(
            result.final_draft,
            result.judge_decisions,
            findings=result.findings,
            resolved_finding_ids=result.resolved_finding_ids,
            citation_issues=result.citation_issues,
        )
        changed = self._reports.get(run_id) != expected
        self._reports[run_id] = expected
        return changed

    def report_bytes(self, run_id: str) -> bytes | None:
        return self._reports.get(run_id)
