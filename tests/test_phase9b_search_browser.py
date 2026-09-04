from __future__ import annotations

import asyncio
import ipaddress
import json
from datetime import UTC, datetime, timedelta

import pytest
from phase9_fixtures import (
    FrozenClock,
    SequentialIds,
    make_real_config,
    make_settings,
    make_snapshot,
)

from researchos.adapters.browser import (
    BoundedBrowserTool,
    BrowserPolicyError,
    BrowserTransportError,
    PinnedHttpTransport,
    TrafilaturaSubprocessExtractor,
    _content_type,
    _read_body,
    canonicalize_browser_url,
    normalize_browser_text,
    validate_public_addresses,
)
from researchos.adapters.memory import InMemoryRunStore, InMemoryTraceSink
from researchos.adapters.real_composition_memory import (
    InMemoryRealCompositionStore,
)
from researchos.adapters.tavily import HttpxTavilyTransport, TavilySearchTool
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.evidence_extractor import EvidenceExtractor
from researchos.application.real_composition import (
    BoundRealCapability,
    RealCompositionManager,
)
from researchos.application.real_tool_dispatch import RealToolDispatchAuthorizer
from researchos.application.run_manager import RunManager
from researchos.configuration.real_settings import (
    default_browser_capability,
    default_tavily_capability,
)
from researchos.domain.agent import AgentContext, AgentObservation
from researchos.domain.contracts import (
    RunInput,
    RunStatus,
    canonical_json_bytes,
    model_sha256,
)
from researchos.domain.identity import sha256_text, stable_hash
from researchos.domain.planning import ResearchTask
from researchos.domain.real_composition import RealCompositionEnvelope
from researchos.domain.real_tools import (
    AuthorizedToolDispatchEnvelope,
    CapabilityDispatchContext,
)
from researchos.domain.runtime import IdempotencyMode, RuntimeResourceAmount
from researchos.domain.tools import (
    AdapterMode,
    BrowserRequest,
    SearchContentKind,
    SearchHit,
    SearchHitV2,
    SearchRequest,
    SearchResult,
    SearchResultV2,
    ToolInvocationRequest,
    ToolInvocationResult,
    ToolInvocationStatus,
)
from researchos.domain.web import (
    BrowserAddressPolicySnapshot,
    HttpBrowserPolicySnapshot,
    TavilyResearchOSPolicyV1,
)
from researchos.interfaces.web import BoundedHttpResponse


class Clock:
    current = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)

    def now(self):
        return self.current


class Signal:
    cancelled = False

    async def wait(self):
        await asyncio.Event().wait()


class CancelledSignal:
    cancelled = True

    async def wait(self):
        return None


class Authorizer:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls = 0

    def authorize(self, envelope):
        self.calls += 1
        if self.reject:
            raise ValueError("denied")


class Secrets:
    def __init__(self) -> None:
        self.calls = 0

    def capability_secret(self, capability_id):
        self.calls += 1
        assert capability_id == "web_search"
        return "secret"


class SearchTransport:
    def __init__(self, response=None) -> None:
        self.calls = []
        self.response = response or BoundedHttpResponse(
            status_code=200,
            headers=(("content-type", "application/json"),),
            body=json.dumps(
                {
                    "results": [
                        {
                            "title": "Result",
                            "url": "https://example.com/page",
                            "content": "provider generated summary",
                            "score": 0.75,
                            "raw_content": "raw-provider-secret",
                        }
                    ],
                    "answer": "provider-answer-secret",
                    "usage": {"credits": 1},
                    "request_id": "provider-secret-id",
                }
            ).encode(),
        )

    async def post_json(self, **values):
        self.calls.append(values)
        return self.response


class BrowserTransport:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    async def get(self, **values):
        self.calls.append(values["url"])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Extractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, content, **values):
        self.calls += 1
        assert content == b"<html>body</html>"
        return "Title", "Extracted body"


def _bound(settings):
    return BoundRealCapability(
        run_id="run_real",
        run_config_hash="a" * 64,
        composition_hash="b" * 64,
        settings=settings,
    )


def _envelope(settings, payload, *, run_id="run_real"):
    task = ResearchTask(
        task_id="task_one",
        perspective_id="perspective_one",
        objective="research",
        expected_outputs=(),
        required_capability_ids=(settings.capability_id,),
    )
    from researchos.application.runtime_transitions import stable_key

    operation_key = stable_key(
        {
            "task_operation_key": "a" * 64,
            "agent_step": 1,
            "capability_id": settings.capability_id,
            "tool_id": settings.tool_id,
            "adapter_id": settings.adapter_id,
            "tool_operation_version": settings.operation_version,
            "safe_input_hash": model_sha256(payload),
        }
    )
    invocation = ToolInvocationRequest(
        invocation_id="invocation_one",
        run_id=run_id,
        task_id=task.task_id,
        attempt_id="attempt_one",
        agent_step=1,
        tool_call_id="call_one",
        tool_operation_key=operation_key,
        deadline=Clock.current + timedelta(minutes=1),
        input=payload,
    )
    context = CapabilityDispatchContext.build(
        run_id=run_id,
        task=task,
        task_id=task.task_id,
        task_contract_hash=model_sha256(task),
        task_operation_key="a" * 64,
        requested_capability_id=settings.capability_id,
        authorized_capability_ids=task.required_capability_ids,
        descriptor_hash=model_sha256(settings.descriptor()),
        tool_operation_key=operation_key,
    )
    return AuthorizedToolDispatchEnvelope(
        invocation=invocation, dispatch_context=context
    )


def test_tavily_sends_only_documented_provider_fields_and_exact_endpoint() -> None:
    settings = default_tavily_capability(max_results=3)
    transport = SearchTransport()
    tool = TavilySearchTool(
        bound=_bound(settings),
        authorizer=Authorizer(),
        compositions=Secrets(),
        transport=transport,
        clock=Clock(),
    )
    result = asyncio.run(
        tool.invoke_authorized(
            _envelope(settings, SearchRequest(query="research", limit=2)), Signal()
        )
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    sent = transport.calls[0]
    assert sent["endpoint"] == "https://api.tavily.com/search"
    assert "schema_version" not in sent["payload"]
    assert "language" not in sent["payload"]
    assert "filter_by_language" not in sent["payload"]
    assert sent["payload"]["safe_search"] is False
    assert sent["payload"]["query"] == "research"
    assert sent["payload"]["max_results"] == 2
    assert (
        result.output.hits[0].content_kind
        is SearchContentKind.PROVIDER_SUMMARY_METADATA
    )
    assert result.output.hits[0].provenance["provider_request_id_hash"] != (
        "provider-secret-id"
    )
    encoded = result.model_dump_json()
    assert "raw-provider-secret" not in encoded
    assert "provider-answer-secret" not in encoded


def test_tavily_denial_occurs_before_secret_or_http() -> None:
    settings = default_tavily_capability()
    authorizer = Authorizer(reject=True)
    secrets = Secrets()
    transport = SearchTransport()
    result = asyncio.run(
        TavilySearchTool(
            bound=_bound(settings),
            authorizer=authorizer,
            compositions=secrets,
            transport=transport,
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, SearchRequest(query="research")), Signal()
        )
    )
    assert result.error.code == "real_tool_dispatch_unauthorized"
    assert result.usage is not None and result.usage.tool_calls == 1
    assert secrets.calls == 0
    assert transport.calls == []


def test_tavily_credential_failure_consumes_one_logical_tool_call() -> None:
    class MissingSecrets:
        def capability_secret(self, capability_id):
            assert capability_id == "web_search"
            return None

    settings = default_tavily_capability()
    transport = SearchTransport()
    result = asyncio.run(
        TavilySearchTool(
            bound=_bound(settings),
            authorizer=Authorizer(),
            compositions=MissingSecrets(),
            transport=transport,
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, SearchRequest(query="research")), Signal()
        )
    )

    assert result.error.code == "tavily_credential_missing"
    assert result.usage is not None and result.usage.tool_calls == 1
    assert transport.calls == []


def test_tampered_composition_blocks_tavily_before_secret_or_http() -> None:
    capability = default_tavily_capability()
    settings = make_settings(capability_settings=(capability,))
    clock = FrozenClock()
    compositions = InMemoryRealCompositionStore(clock=clock)
    runs = InMemoryRunStore()

    class CountingSecrets:
        calls = 0

        def get_secret(self, secret_id):
            self.calls += 1
            return "test-credential"

    secrets = CountingSecrets()
    manager = RealCompositionManager(
        settings=settings,
        secrets=secrets,
        store=compositions,
    )
    lifecycle = RunManager(
        store=runs,
        trace_sink=InMemoryTraceSink(),
        clock=clock,
        id_factory=SequentialIds(),
        integration_guard=manager,
    )
    state = lifecycle.create(
        RunInput(query="research"),
        make_real_config(allowed_capability_ids=("web_search",)),
    )
    lifecycle.transition(state.run_id, RunStatus.PLANNING)
    lifecycle.transition(state.run_id, RunStatus.READY)
    lifecycle.transition(state.run_id, RunStatus.RUNNING)
    bound = manager.bound_capability(state.run_id, "web_search")
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.REAL}))
    authorizer = RealToolDispatchAuthorizer(
        runs=runs,
        compositions=manager,
        registry=registry,
        bound=bound,
    )
    transport = SearchTransport()
    tool = TavilySearchTool(
        bound=bound,
        authorizer=authorizer,
        compositions=manager,
        transport=transport,
        clock=clock,
    )
    registry.register(tool)
    changed = make_settings(
        capability_settings=(default_tavily_capability(max_results=6),)
    )
    changed_snapshot = make_snapshot(
        changed,
        run_id=state.run_id,
        config_hash=state.config_hash,
    )
    compositions._items[state.run_id] = RealCompositionEnvelope.build(
        changed_snapshot,
        recorded_at=clock.now(),
    )
    secrets.calls = 0

    result = asyncio.run(
        tool.invoke_authorized(
            _envelope(
                capability,
                SearchRequest(query="research"),
                run_id=state.run_id,
            ),
            Signal(),
        )
    )

    assert result.error.code == "real_tool_dispatch_unauthorized"
    assert secrets.calls == 0
    assert transport.calls == []


def test_non_running_authority_blocks_browser_before_dns() -> None:
    capability = default_browser_capability()
    settings = make_settings(capability_settings=(capability,))
    clock = FrozenClock()
    compositions = InMemoryRealCompositionStore(clock=clock)
    runs = InMemoryRunStore()

    class Secrets:
        def get_secret(self, secret_id):
            del secret_id
            return "test-credential"

    manager = RealCompositionManager(
        settings=settings,
        secrets=Secrets(),
        store=compositions,
    )
    lifecycle = RunManager(
        store=runs,
        trace_sink=InMemoryTraceSink(),
        clock=clock,
        id_factory=SequentialIds(),
        integration_guard=manager,
    )
    state = lifecycle.create(
        RunInput(query="research"),
        make_real_config(allowed_capability_ids=("web_browser",)),
    )
    lifecycle.transition(state.run_id, RunStatus.PLANNING)
    lifecycle.transition(state.run_id, RunStatus.READY)
    bound = manager.bound_capability(state.run_id, "web_browser")
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.REAL}))
    authorizer = RealToolDispatchAuthorizer(
        runs=runs,
        compositions=manager,
        registry=registry,
        bound=bound,
    )
    transport = BrowserTransport([])
    tool = BoundedBrowserTool(
        bound=bound,
        authorizer=authorizer,
        transport=transport,
        extractor=Extractor(),
        clock=clock,
    )
    registry.register(tool)

    result = asyncio.run(
        tool.invoke_authorized(
            _envelope(
                capability,
                BrowserRequest(url="https://example.com/"),
                run_id=state.run_id,
            ),
            Signal(),
        )
    )

    assert result.error.code == "real_tool_dispatch_unauthorized"
    assert transport.calls == []


def test_tavily_endpoint_identity_is_exact_and_fail_closed() -> None:
    internal = default_tavily_capability().tavily_policy.researchos
    raw = internal.model_dump(mode="json")
    raw["canonical_endpoint_hash"] = "f" * 64
    with pytest.raises(ValueError, match="endpoint identity"):
        TavilyResearchOSPolicyV1.model_validate(raw)


def test_browser_security_policy_controls_are_hashed_and_tamper_closed() -> None:
    baseline = default_browser_capability()
    changed = default_browser_capability(total_invocation_timeout_ms=249_999)
    assert baseline.policy_hash != changed.policy_hash
    assert baseline.operation_version != changed.operation_version
    raw = baseline.browser_policy.model_dump(mode="json")
    raw["address_policy"]["blocked_network_table_hash"] = "f" * 64
    with pytest.raises(ValueError, match="blocked network table hash"):
        HttpBrowserPolicySnapshot.model_validate(raw)
    raw = baseline.browser_policy.model_dump(mode="json")
    raw["mime_allowlist"] = ["text/plain"]
    with pytest.raises(ValueError, match="MIME allowlist"):
        HttpBrowserPolicySnapshot.model_validate(raw)
    raw = baseline.browser_policy.model_dump(mode="json")
    raw["allowed_ports"] = [80, 443, 8080]
    with pytest.raises(ValueError, match="STANDARD profile"):
        HttpBrowserPolicySnapshot.model_validate(raw)


def test_browser_address_policy_change_updates_capability_and_composition() -> None:
    baseline = default_browser_capability()
    networks = baseline.browser_policy.address_policy.blocked_networks + (
        "203.0.114.0/24",
    )
    address_policy = BrowserAddressPolicySnapshot(
        blocked_networks=networks,
        blocked_network_table_hash=stable_hash(networks),
    )
    policy_raw = baseline.browser_policy.model_dump(mode="python")
    policy_raw["address_policy"] = address_policy.model_dump(mode="json")
    changed_policy = HttpBrowserPolicySnapshot(
        **{
            **policy_raw,
            "policy_hash": stable_hash(
                {
                    key: value
                    for key, value in policy_raw.items()
                    if key != "policy_hash"
                }
            ),
        }
    )
    settings_raw = baseline.model_dump(mode="python")
    settings_raw["browser_policy"] = changed_policy
    settings_raw["policy_hash"] = changed_policy.policy_hash
    changed_settings = type(baseline).model_validate(settings_raw)

    assert changed_policy.policy_hash != baseline.policy_hash
    assert changed_settings.operation_version != baseline.operation_version
    changed_composition = make_snapshot(
        make_settings(capability_settings=(changed_settings,))
    )
    baseline_composition = make_snapshot(
        make_settings(capability_settings=(baseline,))
    )
    assert changed_composition.composition_hash != baseline_composition.composition_hash


def test_browser_address_classification_uses_frozen_networks_not_is_global(
    monkeypatch,
) -> None:
    monkeypatch.setattr(ipaddress.IPv4Address, "is_global", property(lambda _: False))

    assert validate_public_addresses(("8.8.8.8",), max_answers=16) == ("8.8.8.8",)


@pytest.mark.parametrize(
    "address",
    (
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
    ),
)
def test_browser_frozen_address_policy_rejects_special_use_ranges(address: str) -> None:
    with pytest.raises(BrowserPolicyError, match="non-public"):
        validate_public_addresses((address,), max_answers=16)


def test_browser_frozen_address_policy_rejects_mixed_dns_and_allows_public_v6() -> None:
    with pytest.raises(BrowserPolicyError, match="non-public"):
        validate_public_addresses(("8.8.8.8", "10.0.0.1"), max_answers=16)
    assert validate_public_addresses(("2606:4700:4700::1111",), max_answers=16) == (
        "2606:4700:4700::1111",
    )


@pytest.mark.requires_real_extra
def test_tavily_http_transport_closes_its_request_scoped_client() -> None:
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_raw(self):
            yield b"{}"

    class Client:
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True
            return False

        def stream(self, *args, **kwargs):
            del args, kwargs
            return Response()

    class Httpx:
        HTTPError = Exception

        @staticmethod
        def Timeout(**values):
            return values

        def __init__(self):
            self.clients: list[Client] = []

        def AsyncClient(self, **values):
            del values
            client = Client()
            self.clients.append(client)
            return client

    settings = default_tavily_capability()
    transport = HttpxTavilyTransport(settings.tavily_policy.researchos)
    fake_httpx = Httpx()
    transport._httpx = fake_httpx

    response = asyncio.run(
        transport.post_json(
            endpoint="https://api.tavily.com/search",
            headers={},
            payload={},
            max_header_bytes=1024,
            max_response_bytes=1024,
            total_timeout_milliseconds=1_000,
            cancellation=Signal(),
        )
    )

    assert response.body == b"{}"
    assert len(fake_httpx.clients) == 1
    assert fake_httpx.clients[0].closed is True


def test_tavily_cancellation_preserves_terminal_cause_and_unknown_usage() -> None:
    class CancelledTransport:
        async def post_json(self, **values):
            raise asyncio.CancelledError

    settings = default_tavily_capability()
    result = asyncio.run(
        TavilySearchTool(
            bound=_bound(settings),
            authorizer=Authorizer(),
            compositions=Secrets(),
            transport=CancelledTransport(),
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, SearchRequest(query="research")), Signal()
        )
    )
    assert result.status is ToolInvocationStatus.CANCELLED
    assert result.error.code == "tavily_cancelled"
    assert result.usage is None
    assert result.usage_certainty.value == "unknown"


def test_tavily_pre_cancelled_signal_performs_no_secret_or_transport_work() -> None:
    class NoSecretLookup:
        def capability_secret(self, capability_id):
            raise AssertionError(f"secret lookup must not occur: {capability_id}")

    class NoTransport:
        async def post_json(self, **values):
            raise AssertionError(f"transport must not run: {values}")

    settings = default_tavily_capability()
    result = asyncio.run(
        TavilySearchTool(
            bound=_bound(settings),
            authorizer=Authorizer(),
            compositions=NoSecretLookup(),
            transport=NoTransport(),
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, SearchRequest(query="research")),
            CancelledSignal(),
        )
    )

    assert result.status is ToolInvocationStatus.CANCELLED
    assert result.error.code == "tavily_cancelled"
    assert result.usage is None


@pytest.mark.requires_real_extra
def test_httpx_tavily_pre_cancelled_signal_creates_no_client() -> None:
    class Httpx:
        HTTPError = Exception

        @staticmethod
        def Timeout(**values):
            return values

        def __init__(self):
            self.client_calls = 0

        def AsyncClient(self, **values):
            del values
            self.client_calls += 1
            raise AssertionError("client creation must not occur")

    settings = default_tavily_capability()
    transport = HttpxTavilyTransport(settings.tavily_policy.researchos)
    fake_httpx = Httpx()
    transport._httpx = fake_httpx

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            transport.post_json(
                endpoint="https://api.tavily.com/search",
                headers={},
                payload={},
                max_header_bytes=1024,
                max_response_bytes=1024,
                total_timeout_milliseconds=1_000,
                cancellation=CancelledSignal(),
            )
        )

    assert fake_httpx.client_calls == 0


def test_tavily_provider_summary_metadata_yields_zero_evidence() -> None:
    now = Clock.current
    output = SearchResultV2(
        adapter_id="tavily_http",
        hits=(
            SearchHitV2(
                locator="https://example.com/",
                url="https://example.com/",
                title="Result",
                snippet="provider summary",
                content_kind=SearchContentKind.PROVIDER_SUMMARY_METADATA,
                retrieved_at=now,
                adapter_id="tavily_http",
            ),
        ),
    )
    context = _agent_context("web_search")
    observation = AgentObservation(
        agent_step=1,
        tool_call_id="call_one",
        capability_id="web_search",
        tool_id="tavily_search",
        adapter_id="tavily_http",
        tool_input_hash="c" * 64,
        tool_operation_key="d" * 64,
        result=ToolInvocationResult(status="succeeded", output=output),
    )
    assert EvidenceExtractor().extract(context, observation) == ()


def test_historical_v1_search_and_invocation_authority_is_byte_stable() -> None:
    request = ToolInvocationRequest(
        invocation_id="invocation_one",
        run_id="run_one",
        task_id="task_one",
        attempt_id="attempt_one",
        agent_step=1,
        tool_call_id="call_one",
        tool_operation_key="a" * 64,
        deadline=datetime(2026, 9, 1, tzinfo=UTC),
        input=SearchRequest(query="research", limit=5),
    )
    result = SearchResult(
        adapter_id="mock_search",
        hits=(
            SearchHit(
                locator="https://example.com/",
                url="https://example.com/",
                title="Example",
                snippet="body",
                retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
                adapter_id="mock_search",
            ),
        ),
    )
    expected_request = (
        b'{"agent_step":1,"attempt_id":"attempt_one","deadline":'
        b'"2026-09-01T00:00:00Z","input":{"input_type":"search","limit":5,'
        b'"query":"research"},"invocation_id":"invocation_one","run_id":'
        b'"run_one","task_id":"task_one","tool_call_id":"call_one",'
        b'"tool_operation_key":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        b'aaaaaaaaaaaaaaaaaaaaaaaa"}'
    )
    expected_result = (
        b'{"adapter_id":"mock_search","hits":[{"adapter_id":"mock_search",'
        b'"locator":"https://example.com/","provenance":{},"retrieved_at":'
        b'"2026-09-01T00:00:00Z","snippet":"body","title":"Example","url":'
        b'"https://example.com/"}],"output_type":"search_result"}'
    )

    assert canonical_json_bytes(request) == expected_request
    assert model_sha256(request) == (
        "0c058b6330bf75c932c11713ecd960d72c0232e3911d017926411c01b8560369"
    )
    assert canonical_json_bytes(result) == expected_result
    assert model_sha256(result) == (
        "4f4a814b26651ccbc5b99eb8ed2447d37191c863c6ab41b70f7ea6add8bfea9c"
    )
    from researchos.application.runtime_transitions import stable_key

    assert stable_key(
        {
            "task_operation_key": "b" * 64,
            "agent_step": 1,
            "capability_id": "web_search",
            "tool_id": "mock_search_tool",
            "adapter_id": "mock_search",
            "tool_operation_version": "v1",
            "safe_input_hash": model_sha256(request.input),
        }
    ) == "039c7552a1fbd5054d6ffa415d3d7783f8823a0c4da44b86110047172677b8b6"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "192.0.0.9",
        "192.88.99.1",
        "224.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "64:ff9b::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_browser_rejects_non_public_dns_answers(address: str) -> None:
    with pytest.raises(BrowserPolicyError):
        validate_public_addresses((address,), max_answers=16)


def test_browser_rejects_mixed_public_and_private_dns_answers() -> None:
    with pytest.raises(BrowserPolicyError):
        validate_public_addresses(("8.8.8.8", "127.0.0.1"), max_answers=16)


def test_browser_url_rejects_userinfo_controls_and_nonstandard_ports() -> None:
    for value in (
        "https://user@example.com/",
        "https://example.com:444/",
        "file:///etc/passwd",
        "https://example.com/\nheader",
        "https://example.com/a b",
        "https://example.com/%zz",
        "https://example.com/café",
    ):
        with pytest.raises(BrowserPolicyError):
            canonicalize_browser_url(value, allowed_ports=(80, 443))


def test_browser_text_normalization_is_nfc_and_newline_deterministic() -> None:
    decomposed = "Cafe\u0301  \r\nnext\t\r\n\r\n"
    composed = "Café\nnext"
    assert normalize_browser_text(decomposed) == composed
    assert sha256_text(normalize_browser_text(decomposed)) == sha256_text(composed)


def test_browser_url_and_redirect_target_are_bounded() -> None:
    with pytest.raises(BrowserPolicyError, match="exceeds bound"):
        canonicalize_browser_url(
            "https://example.com/" + "x" * 2_048,
            allowed_ports=(80, 443),
        )
    settings = default_browser_capability()
    transport = BrowserTransport(
        [
            BoundedHttpResponse(
                status_code=302,
                headers=(("location", "/" + "x" * 2_048),),
                body=b"",
            )
        ]
    )
    result = asyncio.run(
        BoundedBrowserTool(
            bound=_bound(settings),
            authorizer=Authorizer(),
            transport=transport,
            extractor=Extractor(),
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, BrowserRequest(url="https://example.com/")),
            Signal(),
        )
    )

    assert result.error.code == "browser_redirect_invalid"
    assert transport.calls == ["https://example.com/"]


def test_browser_accepts_only_identity_content_encoding() -> None:
    async def check():
        reader = asyncio.StreamReader()
        reader.feed_data(b"compressed")
        reader.feed_eof()
        with pytest.raises(BrowserPolicyError, match="content encoding"):
            await _read_body(
                reader,
                (("content-encoding", "gzip"),),
                100,
                100,
                Signal(),
            )

    asyncio.run(check())


def test_browser_body_reader_enforces_stream_bound() -> None:
    async def check():
        reader = asyncio.StreamReader()
        reader.feed_data(b"01234567890")
        reader.feed_eof()
        with pytest.raises(BrowserPolicyError, match="body exceeds"):
            await _read_body(reader, (), 10, 100, Signal())

    asyncio.run(check())


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "text/html; charset=utf-16", "text/html, text/plain"],
)
def test_browser_rejects_unsupported_or_ambiguous_content_type(
    content_type: str,
) -> None:
    with pytest.raises(BrowserPolicyError):
        _content_type((("content-type", content_type),))


def test_browser_detects_dns_rebinding_before_http_dispatch(monkeypatch) -> None:
    class Resolver:
        values = [("8.8.8.8",), ("1.1.1.1",)]

        async def resolve(self, *args, **kwargs):
            return self.values.pop(0)

    connected = False
    wrote = False

    class Writer:
        def close(self):
            return None

        async def wait_closed(self):
            return None

        def write(self, value):
            nonlocal wrote
            wrote = True

    async def connect(*args, **kwargs):
        nonlocal connected
        assert kwargs["limit"] == 1_000
        connected = True
        return asyncio.StreamReader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    transport = PinnedHttpTransport(Resolver(), max_dns_answers=16)
    with pytest.raises(BrowserTransportError, match="browser_dns_rebinding_detected"):
        asyncio.run(
            transport.get(
                url="https://example.com/",
                max_header_bytes=1_000,
                max_body_bytes=1_000,
                connect_timeout_milliseconds=100,
                read_timeout_milliseconds=100,
                user_agent="ResearchOS-Agent/1.0",
                cancellation=Signal(),
            )
        )
    assert connected is True
    assert wrote is False


def test_browser_rejects_connected_peer_mismatch(monkeypatch) -> None:
    class Resolver:
        async def resolve(self, *args, **kwargs):
            return ("8.8.8.8",)

    class Writer:
        def get_extra_info(self, name):
            assert name == "peername"
            return ("1.1.1.1", 443)

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def connected(*args, **kwargs):
        return asyncio.StreamReader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", connected)
    transport = PinnedHttpTransport(Resolver(), max_dns_answers=16)
    with pytest.raises(BrowserTransportError, match="browser_peer_identity_invalid"):
        asyncio.run(
            transport.get(
                url="https://example.com/",
                max_header_bytes=1_000,
                max_body_bytes=1_000,
                connect_timeout_milliseconds=100,
                read_timeout_milliseconds=100,
                user_agent="ResearchOS-Agent/1.0",
                cancellation=Signal(),
            )
        )


def test_browser_manual_redirect_then_extracts_final_page() -> None:
    settings = default_browser_capability()
    transport = BrowserTransport(
        [
            BoundedHttpResponse(
                status_code=302,
                headers=(("location", "/final"),),
                body=b"",
            ),
            BoundedHttpResponse(
                status_code=200,
                headers=(("content-type", "text/html; charset=utf-8"),),
                body=b"<html>body</html>",
            ),
        ]
    )
    extractor = Extractor()
    authorizer = Authorizer()
    result = asyncio.run(
        BoundedBrowserTool(
            bound=_bound(settings),
            authorizer=authorizer,
            transport=transport,
            extractor=extractor,
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, BrowserRequest(url="https://example.com/start")),
            Signal(),
        )
    )
    assert transport.calls == [
        "https://example.com/start",
        "https://example.com/final",
    ]
    assert authorizer.calls == 2
    assert extractor.calls == 1
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output.final_url == "https://example.com/final"
    assert result.output.content_hash == sha256_text("Extracted body")


def test_browser_incomplete_body_is_mapped_to_retryable_transport_failure(
    monkeypatch,
) -> None:
    class Resolver:
        async def resolve(self, *args, **kwargs):
            return ("8.8.8.8",)

    class Writer:
        def get_extra_info(self, name):
            assert name == "peername"
            return ("8.8.8.8", 443)

        def write(self, value):
            del value

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def connected(*args, **kwargs):
        del args, kwargs
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
            b"Content-Length: 10\r\n\r\nabc"
        )
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connected)
    transport = PinnedHttpTransport(Resolver(), max_dns_answers=16)
    with pytest.raises(
        BrowserTransportError, match="browser_transport_failed"
    ) as caught:
        asyncio.run(
            transport.get(
                url="https://example.com/",
                max_header_bytes=1_000,
                max_body_bytes=1_000,
                connect_timeout_milliseconds=100,
                read_timeout_milliseconds=100,
                user_agent="ResearchOS-Agent/1.0",
                cancellation=Signal(),
            )
    )
    assert caught.value.dispatched is True
    assert caught.value.retryable is True


def test_browser_denial_occurs_before_transport() -> None:
    settings = default_browser_capability()
    transport = BrowserTransport([])
    result = asyncio.run(
        BoundedBrowserTool(
            bound=_bound(settings),
            authorizer=Authorizer(reject=True),
            transport=transport,
            extractor=Extractor(),
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, BrowserRequest(url="https://example.com/")),
            Signal(),
        )
    )
    assert result.error.code == "real_tool_dispatch_unauthorized"
    assert result.usage is not None and result.usage.tool_calls == 1
    assert transport.calls == []


def test_browser_pre_http_and_redirect_failure_consume_one_logical_call() -> None:
    settings = default_browser_capability()
    initial = asyncio.run(
        BoundedBrowserTool(
            bound=_bound(settings),
            authorizer=Authorizer(),
            transport=BrowserTransport(
                [
                    BrowserTransportError(
                        "browser_dns_failed", dispatched=False, retryable=True
                    )
                ]
            ),
            extractor=Extractor(),
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, BrowserRequest(url="https://example.com/")),
            Signal(),
        )
    )
    redirect_transport = BrowserTransport(
        [
            BoundedHttpResponse(
                status_code=302,
                headers=(("location", "https://next.example/"),),
                body=b"",
            ),
            BrowserTransportError(
                "browser_dns_failed", dispatched=False, retryable=True
            ),
        ]
    )
    redirected = asyncio.run(
        BoundedBrowserTool(
            bound=_bound(settings),
            authorizer=Authorizer(),
            transport=redirect_transport,
            extractor=Extractor(),
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, BrowserRequest(url="https://example.com/")),
            Signal(),
        )
    )

    assert initial.usage is not None and initial.usage.tool_calls == 1
    assert redirected.error.code == "browser_dns_failed"
    assert redirected.usage is not None and redirected.usage.tool_calls == 1
    assert redirect_transport.calls == [
        "https://example.com/",
        "https://next.example/",
    ]


def test_browser_total_timeout_is_enforced_by_reserved_policy_bound() -> None:
    class HangingTransport:
        async def get(self, **values):
            del values
            await asyncio.Future()

    settings = default_browser_capability(total_invocation_timeout_ms=1)
    result = asyncio.run(
        BoundedBrowserTool(
            bound=_bound(settings),
            authorizer=Authorizer(),
            transport=HangingTransport(),
            extractor=Extractor(),
            clock=Clock(),
        ).invoke_authorized(
            _envelope(settings, BrowserRequest(url="https://example.com/")),
            Signal(),
        )
    )

    assert result.error.code == "browser_total_timeout"
    assert result.usage is not None and result.usage.tool_calls == 1


@pytest.mark.parametrize("cancelled", [False, True])
def test_extractor_timeout_or_cancellation_kills_and_reaps_worker(
    monkeypatch, cancelled: bool
) -> None:
    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self, request):
            del request
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = Process()

    async def create_process(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    signal = CancelledSignal() if cancelled else Signal()
    expected = asyncio.CancelledError if cancelled else TimeoutError

    with pytest.raises(expected):
        asyncio.run(
            TrafilaturaSubprocessExtractor().extract(
                b"body",
                media_type="text/plain",
                charset="utf-8",
                timeout_milliseconds=1,
                max_characters=100,
                cancellation=signal,
            )
        )

    assert process.killed is True
    assert process.waited is True


@pytest.mark.requires_real_extra
def test_text_extraction_runs_in_bounded_subprocess() -> None:
    title, content = asyncio.run(
        TrafilaturaSubprocessExtractor().extract(
            b"plain body",
            media_type="text/plain",
            charset="utf-8",
            timeout_milliseconds=2_000,
            max_characters=100,
            cancellation=Signal(),
        )
    )
    assert title == "Untitled"
    assert content == "plain body"


@pytest.mark.requires_real_extra
def test_html_extraction_runs_in_bounded_subprocess() -> None:
    title, content = asyncio.run(
        TrafilaturaSubprocessExtractor().extract(
            b"<html><head><title>Example</title></head>"
            b"<body><main>Research body text.</main></body></html>",
            media_type="text/html",
            charset="utf-8",
            timeout_milliseconds=15_000,
            max_characters=1_000,
            cancellation=Signal(),
        )
    )
    assert title == "Example"
    assert "Research body text" in content


def _agent_context(capability_id):
    task = ResearchTask(
        task_id="task_one",
        perspective_id="perspective_one",
        objective="research",
        expected_outputs=(),
        required_capability_ids=(capability_id,),
    )
    return AgentContext(
        run_id="run_real",
        run_revision=3,
        dag_id="dag_one",
        task=task,
        task_id=task.task_id,
        attempt_id="attempt_one",
        attempt_number=1,
        task_operation_key="a" * 64,
        task_attempt_key="b" * 64,
        task_idempotency=IdempotencyMode.IDEMPOTENT,
        deadline=Clock.current + timedelta(minutes=2),
        hard_limits=RuntimeResourceAmount(
            duration_milliseconds=120_000,
            tokens=1_000,
            cost_microunits=1_000_000,
            tool_calls=2,
        ),
        expected_outputs=(),
        authorized_capability_ids=(capability_id,),
    )
