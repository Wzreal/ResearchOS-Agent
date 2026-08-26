"""Deterministic normalization and secret redaction before persistence."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from researchos.application.errors import UnsafePersistenceData
from researchos.domain.contracts import RunInput

_SENSITIVE_KEYS = {
    "authorization",
    "proxyauthorization",
    "cookie",
    "setcookie",
    "apikey",
    "token",
    "accesstoken",
    "refreshtoken",
    "password",
    "secret",
    "credential",
    "privatekey",
}
_AUTHORIZATION = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL_USERINFO = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")


def _normalize_string(value: str) -> str:
    return unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


class PersistenceRedactor:
    """Prepare data before it crosses a persistence boundary."""

    placeholder = "[REDACTED]"

    def normalize_input(self, run_input: RunInput) -> RunInput:
        return RunInput(query=_normalize_string(run_input.query))

    def redact_input(self, run_input: RunInput) -> tuple[RunInput, bool]:
        redacted_query = self.redact_value(run_input.query)
        assert isinstance(redacted_query, str)
        return RunInput(query=redacted_query), redacted_query != run_input.query

    def redact_value(self, value: Any, *, key: str | None = None) -> Any:
        if key is not None and _normalized_key(key) in _SENSITIVE_KEYS:
            return self.placeholder
        if isinstance(value, dict):
            return {
                str(item_key): self.redact_value(item_value, key=str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        if isinstance(value, str):
            result = _PRIVATE_KEY.sub(self.placeholder, value)
            result = _AUTHORIZATION.sub(self.placeholder, result)
            return _URL_USERINFO.sub(r"\1[REDACTED]@", result)
        return deepcopy(value)

    def assert_safe_model(self, model: BaseModel) -> None:
        dumped = model.model_dump(mode="json")
        if self.redact_value(dumped) != dumped:
            raise UnsafePersistenceData(
                "domain object contains data requiring persistence redaction"
            )
