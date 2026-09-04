"""Frozen Phase 9C Zilliz Cloud Free-plan BM25 retrieval policy."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.contracts import ContractModel, SafeId, Sha256
from researchos.domain.identity import sha256_text, stable_hash
from researchos.domain.real_composition import ProviderSuboperationReservation

ZILLIZ_BM25_ADAPTER_VERSION = "zilliz-milvus-bm25-v1"
ZILLIZ_FREE_OR_SERVERLESS_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.serverless\.[a-z0-9-]+\.vectordb\.zillizcloud\.com$"
)


def canonicalize_zilliz_free_or_serverless_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or not ZILLIZ_FREE_OR_SERVERLESS_HOST.fullmatch(parsed.hostname.lower())
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "endpoint is not a Zilliz Cloud Free-or-Serverless serving endpoint"
        )
    return urlunsplit(("https", parsed.hostname.lower(), "/", "", ""))


class ZillizFreePlanAttestation(ContractModel):
    """Operator-provisioned immutable authority for zero-cost Free-plan use.

    A serving hostname proves only the shared Free-or-Serverless endpoint
    family.  The operator must provide this frozen attestation before the
    policy can reserve zero cost; the adapter never derives plan from a URL or
    asks the provider to discover it during a Tool invocation.
    """

    schema_version: Literal[1] = 1
    attestation_version: Literal["operator_provisioned_zilliz_free_plan_v1"] = (
        "operator_provisioned_zilliz_free_plan_v1"
    )
    deployment_plan: Literal["free"] = "free"
    authority_id: SafeId
    attestation_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> ZillizFreePlanAttestation:
        if self.attestation_hash != stable_hash(
            self.model_dump(mode="json", exclude={"attestation_hash"})
        ):
            raise ValueError("Zilliz Free-plan attestation hash differs")
        return self

    @classmethod
    def operator_provisioned(cls, authority_id: str) -> ZillizFreePlanAttestation:
        values = {
            "schema_version": 1,
            "attestation_version": "operator_provisioned_zilliz_free_plan_v1",
            "deployment_plan": "free",
            "authority_id": authority_id,
        }
        return cls(**values, attestation_hash=stable_hash(values))


class ZillizBm25CollectionSchema(ContractModel):
    schema_version: Literal[1] = 1
    schema_contract_version: Literal["researchos-zilliz-bm25-record-v1"] = (
        "researchos-zilliz-bm25-record-v1"
    )
    anns_field: Literal["sparse"] = "sparse"
    document_id_field: Literal["document_id"] = "document_id"
    chunk_id_field: Literal["chunk_id"] = "chunk_id"
    locator_field: Literal["locator"] = "locator"
    content_field: Literal["content"] = "content"
    content_hash_field: Literal["content_hash"] = "content_hash"
    metadata_field: Literal["metadata"] = "metadata"
    schema_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> ZillizBm25CollectionSchema:
        if self.schema_hash != stable_hash(
            self.model_dump(mode="json", exclude={"schema_hash"})
        ):
            raise ValueError("Zilliz collection schema hash differs")
        return self

    @classmethod
    def standard(cls) -> ZillizBm25CollectionSchema:
        values = {
            "schema_version": 1,
            "schema_contract_version": "researchos-zilliz-bm25-record-v1",
            "anns_field": "sparse",
            "document_id_field": "document_id",
            "chunk_id_field": "chunk_id",
            "locator_field": "locator",
            "content_field": "content",
            "content_hash_field": "content_hash",
            "metadata_field": "metadata",
        }
        return cls(**values, schema_hash=stable_hash(values))


class ZillizBm25RetrievalPolicySnapshot(ContractModel):
    schema_version: Literal[1] = 1
    provider_id: Literal["zilliz_cloud"] = "zilliz_cloud"
    provider_profile_id: Literal["zilliz_cloud_free_plan_bm25_v1"] = (
        "zilliz_cloud_free_plan_bm25_v1"
    )
    adapter_id: Literal["zilliz_milvus_bm25"] = "zilliz_milvus_bm25"
    adapter_version: Literal[ZILLIZ_BM25_ADAPTER_VERSION] = ZILLIZ_BM25_ADAPTER_VERSION
    endpoint: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    canonical_endpoint_hash: Sha256
    free_plan_attestation: ZillizFreePlanAttestation
    collection_id: SafeId
    collection_schema: ZillizBm25CollectionSchema
    max_query_bytes: int = Field(default=16_000, ge=1, le=64_000)
    max_results: int = Field(default=20, ge=1, le=100)
    max_content_bytes: int = Field(default=100_000, ge=1, le=1_000_000)
    max_metadata_bytes: int = Field(default=16_384, ge=0, le=65_536)
    total_timeout_ms: int = Field(default=60_000, gt=0, le=300_000)
    credential_slot_id: SafeId = "researchos_zilliz_token"
    provider_reservation: ProviderSuboperationReservation
    policy_hash: Sha256

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_valid(cls, value: str) -> str:
        canonicalize_zilliz_free_or_serverless_endpoint(value)
        return value

    @model_validator(mode="after")
    def identity_is_valid(self) -> ZillizBm25RetrievalPolicySnapshot:
        if self.canonical_endpoint_hash != sha256_text(
            canonicalize_zilliz_free_or_serverless_endpoint(self.endpoint)
        ):
            raise ValueError("Zilliz endpoint hash differs")
        reservation = self.provider_reservation
        if (
            reservation.duration_milliseconds != self.total_timeout_ms
            or reservation.tokens != 0
            or reservation.cost_microunits != 0
            or reservation.cost_currency != "USD"
            or reservation.tool_calls != 1
            or reservation.pricing_policy_id != "zilliz_cloud_free_zero_cost"
        ):
            raise ValueError("Zilliz Free-plan reservation differs from policy")
        if self.policy_hash != self.compute_hash():
            raise ValueError("Zilliz retrieval policy hash differs")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude={"policy_hash"}))
