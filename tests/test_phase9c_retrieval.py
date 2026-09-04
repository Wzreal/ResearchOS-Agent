from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from researchos.adapters.zilliz_retrieval import ZillizBm25RetrievalTool
from researchos.application.real_composition import BoundRealCapability
from researchos.configuration.real_settings import default_managed_retrieval_capability
from researchos.domain.tools import LocalRetrievalRequest


class Clock:
    def now(self):
        return datetime(2026, 9, 4, tzinfo=UTC)


class Signal:
    cancelled = False

    async def wait(self):
        await asyncio.Future()


class Authorizer:
    def __init__(self):
        self.calls = 0

    def authorize(self, envelope):
        del envelope
        self.calls += 1


class Secrets:
    def __init__(self, value="token"):
        self.value = value
        self.calls = 0

    def capability_secret(self, capability_id):
        assert capability_id == "managed_retrieval"
        self.calls += 1
        return self.value


class Transport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def search(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _tool(
    *,
    endpoint="https://cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
    payload=None,
):
    settings = default_managed_retrieval_capability(
        endpoint=endpoint, collection_id="research_docs"
    )
    bound = BoundRealCapability("run", "a" * 64, "b" * 64, settings)
    authorizer, secrets, transport = Authorizer(), Secrets(), Transport(payload or [])
    return (
        ZillizBm25RetrievalTool(
            bound=bound,
            authorizer=authorizer,
            compositions=secrets,
            transport=transport,
            clock=Clock(),
        ),
        authorizer,
        secrets,
        transport,
    )


def _envelope(limit=5):
    return SimpleNamespace(
        invocation=SimpleNamespace(
            input=LocalRetrievalRequest(query="retrieval", limit=limit),
            deadline=Clock().now() + timedelta(seconds=10),
        )
    )


def test_free_profile_has_proven_zero_cost_reservation() -> None:
    settings = default_managed_retrieval_capability(
        endpoint="https://cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
        collection_id="research_docs",
    )
    assert settings.provider_reservation.cost_microunits == 0
    assert (
        settings.provider_reservation.pricing_policy_id == "zilliz_cloud_free_zero_cost"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://cluster-a.aws-us-east-1.vectordb.zillizcloud.com:19530/",
        "https://cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com:443/",
        "https://token@cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
    ],
)
def test_only_free_serving_endpoint_is_accepted(endpoint: str) -> None:
    with pytest.raises(ValueError):
        default_managed_retrieval_capability(endpoint=endpoint, collection_id="docs")


def test_search_is_one_rpc_and_validates_stored_content_hash() -> None:
    content = "original stored text"
    payload = [
        {
            "distance": 1.0,
            "entity": {
                "document_id": "document_one",
                "chunk_id": "chunk_one",
                "locator": "zilliz://document_one/chunk_one",
                "content": content,
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "metadata": {},
            },
        }
    ]
    tool, authorizer, secrets, transport = _tool(payload=payload)
    result = asyncio.run(tool.invoke_authorized(_envelope(), Signal()))
    assert result.status.value == "succeeded"
    assert transport.calls == authorizer.calls == secrets.calls == 1
    assert result.output.hits[0].content == content


def test_hash_mismatch_is_not_admitted_to_evidence_output() -> None:
    payload = [
        {
            "distance": 1.0,
            "entity": {
                "document_id": "document_one",
                "chunk_id": "chunk_one",
                "locator": "zilliz://document_one/chunk_one",
                "content": "original",
                "content_hash": "0" * 64,
                "metadata": {},
            },
        }
    ]
    tool, _, _, transport = _tool(payload=payload)
    result = asyncio.run(tool.invoke_authorized(_envelope(), Signal()))
    assert result.error.code == "retrieval_response_invalid"
    assert transport.calls == 1


def test_provider_failure_is_phase3_retryable_unknown_usage() -> None:
    tool, _, _, _ = _tool(payload=RuntimeError("offline"))
    result = asyncio.run(tool.invoke_authorized(_envelope(), Signal()))
    assert result.error.code == "retrieval_provider_unavailable"
    assert result.error.retryable is True
    assert result.usage is None
