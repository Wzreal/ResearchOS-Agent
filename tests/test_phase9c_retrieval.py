from __future__ import annotations

import asyncio
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from researchos.adapters.zilliz_retrieval import (
    PymilvusZillizBm25Transport,
    ZillizBm25RetrievalTool,
)
from researchos.application.real_composition import BoundRealCapability
from researchos.configuration.real_settings import default_managed_retrieval_capability
from researchos.domain.retrieval import ZillizFreePlanAttestation
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
        endpoint=endpoint,
        collection_id="research_docs",
        free_plan_attestation=ZillizFreePlanAttestation.operator_provisioned(
            "operator_free_plan_authority"
        ),
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


def test_free_plan_attestation_allows_proven_zero_cost_reservation() -> None:
    settings = default_managed_retrieval_capability(
        endpoint="https://cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
        collection_id="research_docs",
        free_plan_attestation=ZillizFreePlanAttestation.operator_provisioned(
            "operator_free_plan_authority"
        ),
    )
    assert settings.provider_reservation.cost_microunits == 0
    assert (
        settings.provider_reservation.pricing_policy_id == "zilliz_cloud_free_zero_cost"
    )


def test_free_plan_attestation_is_frozen_in_policy_identity() -> None:
    common = {
        "endpoint": "https://cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
        "collection_id": "research_docs",
    }
    first = default_managed_retrieval_capability(
        **common,
        free_plan_attestation=ZillizFreePlanAttestation.operator_provisioned(
            "operator_free_plan_authority_a"
        ),
    )
    second = default_managed_retrieval_capability(
        **common,
        free_plan_attestation=ZillizFreePlanAttestation.operator_provisioned(
            "operator_free_plan_authority_b"
        ),
    )
    assert first.policy_hash != second.policy_hash


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://cluster-a.aws-us-east-1.vectordb.zillizcloud.com:19530/",
        "https://cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com:443/",
        "https://token@cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
    ],
)
def test_only_free_or_serverless_serving_endpoint_is_accepted(endpoint: str) -> None:
    with pytest.raises(ValueError):
        default_managed_retrieval_capability(
            endpoint=endpoint,
            collection_id="docs",
            free_plan_attestation=ZillizFreePlanAttestation.operator_provisioned(
                "operator_free_plan_authority"
            ),
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://free-cluster.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
        "https://serverless-cluster.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
    ],
)
def test_endpoint_family_cannot_grant_zero_cost_without_free_plan_authority(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="Free-plan attestation"):
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


@pytest.mark.parametrize("response", [[[]], RuntimeError("offline"), []])
def test_pymilvus_transport_closes_client_on_every_terminal_path(response) -> None:
    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False

        def search(self, **kwargs):
            if isinstance(response, Exception):
                raise response
            return response

        def close(self):
            self.closed = True

    clients = []

    def factory(**kwargs):
        client = Client(**kwargs)
        clients.append(client)
        return client

    transport = PymilvusZillizBm25Transport(factory)
    call = dict(
        endpoint="https://cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com/",
        credential="token",
        collection_id="docs",
        anns_field="sparse",
        query="q",
        limit=1,
        output_fields=("content",),
        timeout_seconds=1.0,
    )
    if isinstance(response, list) and response == [[]]:
        assert transport.search(**call) == []
    else:
        with pytest.raises((RuntimeError, ValueError)):
            transport.search(**call)
    assert clients[0].closed is True


def test_outer_cancellation_returns_unknown_and_worker_finishes_cleanly() -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockingTransport:
        def search(self, **kwargs):
            del kwargs
            started.set()
            release.wait(timeout=2)
            finished.set()
            return []

    async def invoke_then_cancel():
        tool, _, _, _ = _tool(payload=[])
        tool._transport = BlockingTransport()  # narrow test transport injection
        task = asyncio.create_task(tool.invoke_authorized(_envelope(), Signal()))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        result = await task
        assert result.status.value == "cancelled"
        assert result.usage is None
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)

    asyncio.run(invoke_then_cancel())
