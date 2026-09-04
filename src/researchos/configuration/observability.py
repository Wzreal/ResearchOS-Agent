"""Optional OTLP/HTTP configuration; secret values never enter settings."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from researchos.domain.contracts import ContractModel, SafeId

_HEADER_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "accept",
        "connection",
        "transfer-encoding",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
)


class OtlpHttpSettings(ContractModel):
    exporter_id: SafeId = "otlp_http_protobuf_v1"
    traces_endpoint: str
    auth_header_name: str | None = None
    auth_credential_slot_id: SafeId | None = None
    max_request_bytes: int = Field(default=262_144, ge=1, le=4_194_304)
    max_response_bytes: int = Field(default=65_536, ge=1, le=1_048_576)
    connect_timeout_ms: int = Field(default=1_000, ge=1, le=60_000)
    read_timeout_ms: int = Field(default=2_000, ge=1, le=60_000)
    write_timeout_ms: int = Field(default=2_000, ge=1, le=60_000)
    pool_timeout_ms: int = Field(default=1_000, ge=1, le=60_000)
    total_timeout_ms: int = Field(default=2_500, ge=1, le=60_000)

    @field_validator("traces_endpoint")
    @classmethod
    def endpoint_is_direct_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OTLP endpoint must be direct HTTPS without credentials")
        if not parsed.path.endswith("/v1/traces"):
            raise ValueError("OTLP endpoint must target /v1/traces")
        return value

    @field_validator("auth_header_name")
    @classmethod
    def header_name_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _HEADER_TOKEN.fullmatch(value) or value.lower() in _FORBIDDEN_HEADERS:
            raise ValueError("OTLP auth header name is forbidden")
        return value

    @model_validator(mode="after")
    def auth_pair_is_exact(self) -> OtlpHttpSettings:
        if (self.auth_header_name is None) != (self.auth_credential_slot_id is None):
            raise ValueError(
                "OTLP auth header name and secret slot are required together"
            )
        return self


def load_otlp_http_settings(
    environ: dict[str, str] | None = None,
) -> OtlpHttpSettings | None:
    values = environ if environ is not None else __import__("os").environ
    endpoint = values.get("RESEARCHOS_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint:
        return None
    header_name = values.get("RESEARCHOS_OTLP_AUTH_HEADER_NAME", "").strip() or None
    if (
        header_name is not None
        and not values.get("RESEARCHOS_OTLP_AUTH_HEADER_VALUE", "").strip()
    ):
        raise ValueError("required OTLP credential is absent")
    return OtlpHttpSettings(
        traces_endpoint=endpoint,
        auth_header_name=header_name,
        auth_credential_slot_id=(
            "researchos_otlp_auth_header_value" if header_name else None
        ),
    )
