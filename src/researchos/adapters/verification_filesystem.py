"""Filesystem Phase 6 authority: verification.json then derived report.md."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from researchos.adapters._atomic_file import AtomicWriteFailure, atomic_replace_bytes
from researchos.application.errors import (
    VerificationArtifactConflict,
    VerificationPersistenceError,
)
from researchos.application.verification_publisher import render_markdown
from researchos.domain.contracts import canonical_json_bytes
from researchos.domain.synthesis import VerificationResult
from researchos.security.redaction import PersistenceRedactor


class FilesystemVerificationArtifactStore:
    def __init__(
        self, root: Path, *, fault: Callable[[str], None] | None = None
    ) -> None:
        self._root = root.resolve()
        self._fault = fault or (lambda _stage: None)
        self._redactor = PersistenceRedactor()

    def _paths(self, run_id: str) -> tuple[Path, Path]:
        run_root = (self._root / run_id).resolve()
        if not run_root.is_relative_to(self._root):
            raise VerificationArtifactConflict("verification path escapes root")
        return run_root / "verification.json", run_root / "report.md"

    def load(self, run_id: str) -> VerificationResult | None:
        authority, _ = self._paths(run_id)
        if not authority.exists():
            return None
        try:
            result = VerificationResult.model_validate_json(authority.read_bytes())
            self._redactor.assert_safe_model(result)
        except (OSError, ValidationError, ValueError) as exc:
            raise VerificationArtifactConflict(
                "authoritative artifact is corrupt"
            ) from exc
        if result.run_id != run_id:
            raise VerificationArtifactConflict("authoritative artifact run differs")
        expected_markdown = render_markdown(
            result.final_draft,
            result.judge_decisions,
            findings=result.findings,
            resolved_finding_ids=result.resolved_finding_ids,
            citation_issues=result.citation_issues,
            disposition=result.disposition,
            acquisition_requests=result.acquisition_requests,
            omitted_claim_ids=result.omitted_claim_ids,
        )
        if hashlib.sha256(expected_markdown).hexdigest() != result.markdown_sha256:
            raise VerificationArtifactConflict("authoritative Markdown hash differs")
        return result

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
        authority, report = self._paths(result.run_id)
        current = self.load(result.run_id)
        if current is not None and current.verification_id == result.verification_id:
            if current != result:
                raise VerificationArtifactConflict(
                    "same verification identity has different canonical artifact"
                )
            self._write_report(result.run_id, report, markdown)
            return
        if (
            None if current is None else current.verification_id
        ) != expected_prior_verification_id:
            raise VerificationArtifactConflict("authoritative artifact CAS conflict")
        try:
            atomic_replace_bytes(
                authority,
                canonical_json_bytes(result),
                fault=lambda stage: self._fault(f"authority.{stage}"),
            )
        except AtomicWriteFailure as exc:
            raise VerificationPersistenceError(
                "authoritative artifact write failed",
                run_id=result.run_id,
                authority_committed=exc.replaced,
                report_committed=False,
                artifact_committed=exc.replaced,
            ) from exc
        self._write_report(result.run_id, report, markdown)

    def _write_report(self, run_id: str, path: Path, markdown: bytes) -> None:
        try:
            atomic_replace_bytes(
                path,
                markdown,
                fault=lambda stage: self._fault(f"report.{stage}"),
            )
        except AtomicWriteFailure as exc:
            raise VerificationPersistenceError(
                "derived report write failed",
                run_id=run_id,
                authority_committed=True,
                report_committed=exc.replaced,
                artifact_committed=True,
            ) from exc

    def reconcile_report(self, run_id: str) -> bool:
        result = self.load(run_id)
        if result is None:
            return False
        _, report = self._paths(run_id)
        expected = render_markdown(
            result.final_draft,
            result.judge_decisions,
            findings=result.findings,
            resolved_finding_ids=result.resolved_finding_ids,
            citation_issues=result.citation_issues,
            disposition=result.disposition,
            acquisition_requests=result.acquisition_requests,
            omitted_claim_ids=result.omitted_claim_ids,
        )
        if report.exists() and report.read_bytes() == expected:
            return False
        self._write_report(run_id, report, expected)
        return True
