"""Phase 9B immutable REAL Search and Browser policy contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from researchos.domain.contracts import ContractModel, SafeId, Sha256
from researchos.domain.identity import sha256_text, stable_hash
from researchos.domain.real_composition import ProviderSuboperationReservation

TAVILY_STANDARD_ENDPOINT = "https://api.tavily.com"
TAVILY_ADAPTER_VERSION = "tavily-http-v1"
BROWSER_ADAPTER_VERSION = "bounded-http-browser-v1"
BROWSER_BLOCKED_CIDRS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "64:ff9b::/96",
    "64:ff9b:1::/48",
    "100::/64",
    "2001::/23",
    "2001:2::/48",
    "2001:3::/32",
    "2001:4:112::/48",
    "2001:10::/28",
    "2001:20::/28",
    "2001:db8::/32",
    "2002::/16",
    "3fff::/20",
    "5f00::/16",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)
BROWSER_BLOCKED_CIDR_TABLE_HASH = stable_hash(BROWSER_BLOCKED_CIDRS)
BROWSER_ALLOWED_MIME_TYPES = ("text/html", "text/plain")
BROWSER_ALLOWED_CHARSETS = ("iso-8859-1", "us-ascii", "utf-8")


class BrowserAddressPolicySnapshot(ContractModel):
    """Frozen special-use network authority used for Browser SSRF decisions."""

    schema_version: Literal[1] = 1
    classification_version: Literal["researchos-special-use-network-v1"] = (
        "researchos-special-use-network-v1"
    )
    blocked_networks: tuple[str, ...] = BROWSER_BLOCKED_CIDRS
    blocked_network_table_hash: Sha256
    ipv4_mapped_ipv6_policy: Literal["map-to-ipv4-then-classify-v1"] = (
        "map-to-ipv4-then-classify-v1"
    )
    mixed_dns_policy: Literal["reject-any-blocked-answer-v1"] = (
        "reject-any-blocked-answer-v1"
    )

    @model_validator(mode="after")
    def identity_is_valid(self) -> BrowserAddressPolicySnapshot:
        import ipaddress

        canonical: list[str] = []
        for value in self.blocked_networks:
            try:
                network = ipaddress.ip_network(value, strict=True)
            except ValueError as exc:
                raise ValueError("Browser blocked network is invalid") from exc
            if str(network) != value:
                raise ValueError("Browser blocked network is not canonical")
            canonical.append(value)
        if len(canonical) != len(set(canonical)):
            raise ValueError("Browser blocked networks contain duplicates")
        if self.blocked_network_table_hash != stable_hash(tuple(canonical)):
            raise ValueError("Browser blocked network table hash differs")
        return self

    @classmethod
    def standard(cls) -> BrowserAddressPolicySnapshot:
        return cls(
            blocked_network_table_hash=BROWSER_BLOCKED_CIDR_TABLE_HASH,
        )


class TavilyProviderFeatureProfile(StrEnum):
    STANDARD = "standard"


class TavilyProviderRequestPolicyV1(ContractModel):
    schema_version: Literal[1] = 1
    search_depth: Literal["basic"] = "basic"
    chunks_per_source: Literal[1] = 1
    topic: Literal["general"] = "general"
    max_results: int = Field(default=5, ge=1, le=20)
    auto_parameters: Literal[False] = False
    exact_match: Literal[False] = False
    safe_search: Literal[False] = False
    include_answer: Literal[False] = False
    include_raw_content: Literal[False] = False
    include_images: Literal[False] = False
    include_image_descriptions: Literal[False] = False
    include_favicon: Literal[False] = False
    include_usage: Literal[True] = True
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    country: None = None
    time_range: None = None
    start_date: None = None
    end_date: None = None

    @model_validator(mode="after")
    def unsupported_filters_are_empty(self) -> TavilyProviderRequestPolicyV1:
        if self.include_domains or self.exclude_domains:
            raise ValueError("Phase 9B Tavily domain filters are unsupported")
        return self


class TavilyPricingSafetyProfile(ContractModel):
    schema_version: Literal[1] = 1
    profile_version: Literal["tavily-standard-basic-2026-09-v1"] = (
        "tavily-standard-basic-2026-09-v1"
    )
    provider_profile_id: Literal["tavily_standard"] = "tavily_standard"
    maximum_credits_per_call: Literal[1] = 1
    minimum_microunits_per_credit: Literal[100_000] = 100_000
    billing_currency: Literal["USD"] = "USD"
    profile_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> TavilyPricingSafetyProfile:
        if self.profile_hash != self.compute_hash():
            raise ValueError("Tavily pricing safety profile hash differs")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude={"profile_hash"}))

    @classmethod
    def standard(cls) -> TavilyPricingSafetyProfile:
        values = {
            "schema_version": 1,
            "profile_version": "tavily-standard-basic-2026-09-v1",
            "provider_profile_id": "tavily_standard",
            "maximum_credits_per_call": 1,
            "minimum_microunits_per_credit": 100_000,
            "billing_currency": "USD",
        }
        return cls(**values, profile_hash=stable_hash(values))


class TavilyResearchOSPolicyV1(ContractModel):
    schema_version: Literal[1] = 1
    canonicalization_version: Literal["tavily-researchos-policy-v1"] = (
        "tavily-researchos-policy-v1"
    )
    provider_id: Literal["tavily"] = "tavily"
    provider_profile_id: Literal["tavily_standard"] = "tavily_standard"
    feature_profile: Literal[TavilyProviderFeatureProfile.STANDARD] = (
        TavilyProviderFeatureProfile.STANDARD
    )
    adapter_id: Literal["tavily_http"] = "tavily_http"
    adapter_version: Literal[TAVILY_ADAPTER_VERSION] = TAVILY_ADAPTER_VERSION
    endpoint_canonicalization_version: Literal["tavily-endpoint-v1"] = (
        "tavily-endpoint-v1"
    )
    canonical_endpoint_hash: Sha256
    request_schema_version: Literal["tavily-search-request-v1"] = (
        "tavily-search-request-v1"
    )
    response_schema_version: Literal["tavily-search-response-v1"] = (
        "tavily-search-response-v1"
    )
    url_canonicalization_version: Literal["web-url-v1"] = "web-url-v1"
    duplicate_policy_version: Literal["reject-duplicate-url-v1"] = (
        "reject-duplicate-url-v1"
    )
    error_mapping_version: Literal["tavily-error-map-v1"] = "tavily-error-map-v1"
    language_filtering_enabled: Literal[False] = False
    max_query_characters: int = Field(default=4_000, ge=1, le=4_000)
    max_query_bytes: int = Field(default=16_000, ge=1, le=64_000)
    max_request_bytes: int = Field(default=65_536, ge=1, le=1_048_576)
    max_header_bytes: int = Field(default=16_384, ge=1, le=65_536)
    max_response_bytes: int = Field(default=1_048_576, ge=1, le=8_388_608)
    connect_timeout_ms: int = Field(default=10_000, gt=0, le=120_000)
    read_timeout_ms: int = Field(default=30_000, gt=0, le=300_000)
    write_timeout_ms: int = Field(default=10_000, gt=0, le=120_000)
    pool_timeout_ms: int = Field(default=10_000, gt=0, le=120_000)
    total_timeout_ms: int = Field(default=60_000, gt=0, le=300_000)
    credential_slot_id: SafeId = "researchos_tavily_api_key"
    pricing_profile: TavilyPricingSafetyProfile
    microunits_per_credit_upper_bound: int = Field(ge=100_000)
    pricing_policy_id: Literal["tavily_credit_upper_bound"] = (
        "tavily_credit_upper_bound"
    )
    pricing_policy_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ] = "upper-bound-v1"
    pricing_rules_hash: Sha256
    provider_reservation: ProviderSuboperationReservation

    @model_validator(mode="after")
    def pricing_is_safe(self) -> TavilyResearchOSPolicyV1:
        if self.canonical_endpoint_hash != sha256_text(
            f"{TAVILY_STANDARD_ENDPOINT}/"
        ):
            raise ValueError("Tavily endpoint identity differs from standard endpoint")
        if self.pricing_profile != TavilyPricingSafetyProfile.standard():
            raise ValueError("unsupported Tavily pricing safety profile")
        if (
            self.microunits_per_credit_upper_bound
            < self.pricing_profile.minimum_microunits_per_credit
        ):
            raise ValueError("Tavily credit rate is below safety floor")
        expected_rules = stable_hash(
            {
                "pricing_policy_id": self.pricing_policy_id,
                "pricing_policy_version": self.pricing_policy_version,
                "pricing_profile_hash": self.pricing_profile.profile_hash,
                "microunits_per_credit_upper_bound": (
                    self.microunits_per_credit_upper_bound
                ),
            }
        )
        if self.pricing_rules_hash != expected_rules:
            raise ValueError("Tavily pricing rules hash differs")
        expected_cost = (
            self.pricing_profile.maximum_credits_per_call
            * self.microunits_per_credit_upper_bound
        )
        if (
            self.provider_reservation.cost_microunits != expected_cost
            or self.provider_reservation.cost_currency
            != self.pricing_profile.billing_currency
            or self.provider_reservation.tool_calls != 1
            or self.provider_reservation.tokens != 0
            or self.provider_reservation.duration_milliseconds
            != self.total_timeout_ms
            or self.provider_reservation.pricing_rules_hash != expected_rules
        ):
            raise ValueError("Tavily provider reservation differs from policy")
        return self


class TavilySearchPolicySnapshot(ContractModel):
    schema_version: Literal[1] = 1
    provider_request: TavilyProviderRequestPolicyV1
    researchos: TavilyResearchOSPolicyV1
    policy_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> TavilySearchPolicySnapshot:
        if self.policy_hash != self.compute_hash():
            raise ValueError("Tavily policy hash differs")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude={"policy_hash"}))


class HttpBrowserPolicySnapshot(ContractModel):
    schema_version: Literal[1] = 1
    canonicalization_version: Literal["bounded-http-browser-policy-v1"] = (
        "bounded-http-browser-policy-v1"
    )
    adapter_id: Literal["bounded_http_browser"] = "bounded_http_browser"
    adapter_version: Literal[BROWSER_ADAPTER_VERSION] = BROWSER_ADAPTER_VERSION
    allowed_schemes: tuple[Literal["http", "https"], ...] = ("http", "https")
    allowed_ports: tuple[int, ...] = (80, 443)
    max_url_characters: int = Field(default=2_048, ge=1, le=8_192)
    max_url_bytes: int = Field(default=8_192, ge=1, le=32_768)
    max_redirects: int = Field(default=5, ge=0, le=10)
    redirect_policy_version: Literal["manual-revalidate-every-hop-v1"] = (
        "manual-revalidate-every-hop-v1"
    )
    max_dns_answers: int = Field(default=16, ge=1, le=64)
    dns_rebinding_policy_version: Literal[
        "two-resolve-exact-set-peer-v1"
    ] = "two-resolve-exact-set-peer-v1"
    address_policy: BrowserAddressPolicySnapshot
    max_header_bytes: int = Field(default=65_536, ge=1, le=262_144)
    max_body_bytes: int = Field(default=2_097_152, ge=1, le=8_388_608)
    max_extracted_characters: int = Field(default=100_000, ge=1, le=100_000)
    connect_timeout_ms: int = Field(default=10_000, gt=0, le=120_000)
    read_timeout_ms: int = Field(default=30_000, gt=0, le=300_000)
    extraction_timeout_ms: int = Field(default=10_000, gt=0, le=120_000)
    total_invocation_timeout_ms: int = Field(default=250_000, gt=0, le=300_000)
    request_target_canonicalization_version: Literal[
        "ascii-canonical-target-reject-nonascii-v1"
    ] = "ascii-canonical-target-reject-nonascii-v1"
    mime_allowlist: tuple[Literal["text/html", "text/plain"], ...] = (
        "text/html",
        "text/plain",
    )
    charset_allowlist: tuple[
        Literal["iso-8859-1", "us-ascii", "utf-8"], ...
    ] = ("iso-8859-1", "us-ascii", "utf-8")
    content_encoding_policy: Literal["identity-only-v1"] = "identity-only-v1"
    extraction_policy_version: Literal["trafilatura-extract-v1"] = (
        "trafilatura-extract-v1"
    )
    text_normalization_version: Literal["nfc-newline-rstrip-v1"] = (
        "nfc-newline-rstrip-v1"
    )
    user_agent: Literal["ResearchOS-Agent/1.0"] = "ResearchOS-Agent/1.0"
    provider_reservation: ProviderSuboperationReservation
    policy_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> HttpBrowserPolicySnapshot:
        if self.allowed_schemes != ("http", "https"):
            raise ValueError("Browser schemes differ from supported behavior")
        if self.allowed_ports != tuple(sorted(set(self.allowed_ports))):
            raise ValueError("Browser ports must be unique and sorted")
        if any(port not in (80, 443) for port in self.allowed_ports):
            raise ValueError("Browser ports differ from the STANDARD profile")
        if self.max_url_bytes < self.max_url_characters:
            raise ValueError("Browser URL byte bound is below character bound")
        if self.mime_allowlist != BROWSER_ALLOWED_MIME_TYPES:
            raise ValueError("Browser MIME allowlist differs from supported behavior")
        if self.charset_allowlist != BROWSER_ALLOWED_CHARSETS:
            raise ValueError(
                "Browser charset allowlist differs from supported behavior"
            )
        if (
            self.provider_reservation.cost_microunits != 0
            or self.provider_reservation.tokens != 0
            or self.provider_reservation.tool_calls != 1
            or self.provider_reservation.cost_currency != "USD"
            or self.provider_reservation.duration_milliseconds
            != self.total_invocation_timeout_ms
        ):
            raise ValueError("Browser provider reservation differs from policy")
        if self.policy_hash != self.compute_hash():
            raise ValueError("Browser policy hash differs")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude={"policy_hash"}))
