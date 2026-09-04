"""SSRF-resistant bounded HTTP Browser REAL Tool.

The transport connects to a policy-approved numeric address while preserving
the original hostname for HTTP Host and TLS SNI/certificate verification.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import sys
import unicodedata
from contextlib import suppress
from pathlib import Path
from time import monotonic
from urllib.parse import urljoin, urlsplit, urlunsplit

from researchos.application.real_composition import BoundRealCapability
from researchos.application.real_tool_dispatch import RealToolDispatchAuthorizer
from researchos.domain.identity import sha256_text
from researchos.domain.runtime import UsageCertainty
from researchos.domain.tools import (
    BrowserRequest,
    BrowserResult,
    ToolError,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolUsage,
)
from researchos.domain.web import (
    BROWSER_ALLOWED_CHARSETS,
    BROWSER_ALLOWED_MIME_TYPES,
    BrowserAddressPolicySnapshot,
)
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.runtime import CancellationSignal
from researchos.interfaces.web import (
    BoundedHttpResponse,
    BrowserDocumentExtractor,
    BrowserHttpTransport,
    HostResolver,
)


class BrowserPolicyError(ValueError):
    """The URL or response violates the frozen Browser policy."""


class BrowserTransportError(Exception):
    def __init__(self, code: str, *, dispatched: bool, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.dispatched = dispatched
        self.retryable = retryable


_STANDARD_ADDRESS_POLICY = BrowserAddressPolicySnapshot.standard()
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in _STANDARD_ADDRESS_POLICY.blocked_networks
)


def canonicalize_browser_url(
    value: str,
    *,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
    allowed_ports: tuple[int, ...],
    max_characters: int = 2_048,
    max_bytes: int = 8_192,
) -> str:
    if (
        not value
        or len(value) > max_characters
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise BrowserPolicyError("browser URL exceeds bound")
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character.isspace()
        or ord(character) > 127
        for character in value
    ):
        raise BrowserPolicyError("browser URL contains control characters")
    _validate_percent_escapes(value)
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes or not parsed.hostname:
        raise BrowserPolicyError("browser URL must be absolute HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserPolicyError("browser URL cannot contain userinfo")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port or (443 if scheme == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise BrowserPolicyError("browser URL host or port is invalid") from exc
    if not host or port not in allowed_ports:
        raise BrowserPolicyError("browser URL port is not allowed")
    bracketed = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    authority = bracketed if port == default_port else f"{bracketed}:{port}"
    return urlunsplit((scheme, authority, parsed.path or "/", parsed.query, ""))


def _validate_percent_escapes(value: str) -> None:
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or any(
                item not in "0123456789abcdefABCDEF"
                for item in value[index + 1 : index + 3]
            )
        ):
            raise BrowserPolicyError("browser URL contains invalid percent encoding")


def validate_public_addresses(
    values: tuple[str, ...], *, max_answers: int, blocked_networks=_BLOCKED_NETWORKS
) -> tuple[str, ...]:
    if not values or len(values) > max_answers:
        raise BrowserPolicyError("DNS answer count violates policy")
    canonical: set[str] = set()
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise BrowserPolicyError("DNS returned an invalid address") from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if any(
            address.version == network.version and address in network
            for network in blocked_networks
        ):
            raise BrowserPolicyError("DNS returned a non-public address")
        canonical.add(address.compressed)
    if not canonical or len(canonical) > max_answers:
        raise BrowserPolicyError("DNS answer count violates policy")
    return tuple(sorted(canonical))


class SystemHostResolver:
    async def resolve(
        self,
        host: str,
        port: int,
        *,
        cancellation: CancellationSignal,
    ) -> tuple[str, ...]:
        if cancellation.cancelled:
            raise asyncio.CancelledError
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return tuple(sorted({record[4][0] for record in records}))


class PinnedHttpTransport:
    """One-hop HTTP/1.1 transport with DNS rebinding and peer checks."""

    def __init__(
        self,
        resolver: HostResolver,
        *,
        max_dns_answers: int,
        address_policy: BrowserAddressPolicySnapshot | None = None,
    ) -> None:
        address_policy = address_policy or _STANDARD_ADDRESS_POLICY
        self._resolver = resolver
        self._max_dns_answers = max_dns_answers
        self._blocked_networks = tuple(
            ipaddress.ip_network(value) for value in address_policy.blocked_networks
        )

    async def get(self, **values: object) -> BoundedHttpResponse:
        url = str(values["url"])
        cancellation = values["cancellation"]
        assert hasattr(cancellation, "wait")
        parsed = urlsplit(url)
        host = parsed.hostname
        assert host is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            first = validate_public_addresses(
                await self._resolver.resolve(host, port, cancellation=cancellation),
                max_answers=self._max_dns_answers,
                blocked_networks=self._blocked_networks,
            )
        except BrowserPolicyError as exc:
            raise BrowserTransportError(
                "browser_address_rejected", dispatched=False, retryable=False
            ) from exc
        except OSError as exc:
            raise BrowserTransportError(
                "browser_dns_failed", dispatched=False, retryable=True
            ) from exc
        address = first[0]
        tls_context = ssl.create_default_context() if parsed.scheme == "https" else None
        if tls_context is not None:
            tls_context.set_alpn_protocols(["http/1.1"])
        connect = asyncio.open_connection(
            address,
            port,
            ssl=tls_context,
            server_hostname=host if parsed.scheme == "https" else None,
            limit=int(values["max_header_bytes"]),
        )
        try:
            reader, writer = await _await_with_cancel(
                connect,
                cancellation,
                int(values["connect_timeout_milliseconds"]),
            )
        except TimeoutError as exc:
            raise BrowserTransportError(
                "browser_connect_timeout", dispatched=False, retryable=True
            ) from exc
        except ssl.SSLError as exc:
            raise BrowserTransportError(
                "browser_tls_failed", dispatched=False, retryable=False
            ) from exc
        except OSError as exc:
            raise BrowserTransportError(
                "browser_connect_failed", dispatched=False, retryable=True
            ) from exc
        try:
            second = validate_public_addresses(
                await self._resolver.resolve(host, port, cancellation=cancellation),
                max_answers=self._max_dns_answers,
                blocked_networks=self._blocked_networks,
            )
        except BrowserPolicyError as exc:
            writer.close()
            await writer.wait_closed()
            raise BrowserTransportError(
                "browser_address_rejected", dispatched=False, retryable=False
            ) from exc
        except OSError as exc:
            writer.close()
            await writer.wait_closed()
            raise BrowserTransportError(
                "browser_dns_failed", dispatched=False, retryable=True
            ) from exc
        except asyncio.CancelledError:
            writer.close()
            await writer.wait_closed()
            raise
        if first != second:
            writer.close()
            await writer.wait_closed()
            raise BrowserTransportError(
                "browser_dns_rebinding_detected", dispatched=False, retryable=False
            )
        peer = writer.get_extra_info("peername")
        try:
            peer_value = ipaddress.ip_address(peer[0])
            if isinstance(peer_value, ipaddress.IPv6Address) and peer_value.ipv4_mapped:
                peer_value = peer_value.ipv4_mapped
            peer_address = peer_value.compressed
        except (TypeError, ValueError, IndexError) as exc:
            writer.close()
            await writer.wait_closed()
            raise BrowserTransportError(
                "browser_peer_identity_invalid", dispatched=False, retryable=False
            ) from exc
        if peer_address != address:
            writer.close()
            await writer.wait_closed()
            raise BrowserTransportError(
                "browser_peer_identity_invalid", dispatched=False, retryable=False
            )
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        host_authority = f"[{host}]" if ":" in host else host
        default_port = 443 if parsed.scheme == "https" else 80
        authority = (
            host_authority
            if port == default_port
            else f"{host_authority}:{port}"
        )
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {authority}\r\n"
            f"User-Agent: {values['user_agent']}\r\nAccept: text/html,text/plain\r\n"
            "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        try:
            writer.write(request)
            await writer.drain()
            header_block = await _read_headers(
                reader,
                int(values["max_header_bytes"]),
                int(values["read_timeout_milliseconds"]),
                cancellation,
            )
            status, headers = _parse_headers(header_block)
            body = await _read_body(
                reader,
                headers,
                int(values["max_body_bytes"]),
                int(values["read_timeout_milliseconds"]),
                cancellation,
                content_encoding_policy=str(
                    values.get("content_encoding_policy", "identity-only-v1")
                ),
            )
            return BoundedHttpResponse(status_code=status, headers=headers, body=body)
        except BrowserPolicyError as exc:
            raise BrowserTransportError(
                "browser_response_policy_violation", dispatched=True, retryable=False
            ) from exc
        except TimeoutError as exc:
            raise BrowserTransportError(
                "browser_read_timeout", dispatched=True, retryable=True
            ) from exc
        except (OSError, asyncio.IncompleteReadError) as exc:
            raise BrowserTransportError(
                "browser_transport_failed", dispatched=True, retryable=True
            ) from exc
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


async def _await_with_cancel(awaitable, cancellation, timeout_ms: int):
    operation = asyncio.create_task(awaitable)
    cancelled = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            {operation, cancelled},
            timeout=timeout_ms / 1_000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancelled in done:
            raise asyncio.CancelledError
        if operation not in done:
            raise TimeoutError
        return operation.result()
    finally:
        for task in (operation, cancelled):
            if not task.done():
                task.cancel()
        for task in (operation, cancelled):
            with suppress(asyncio.CancelledError, Exception):
                await task


async def _read_headers(reader, limit, timeout, cancellation) -> bytes:
    try:
        value = await _await_with_cancel(
            reader.readuntil(b"\r\n\r\n"), cancellation, timeout
        )
    except asyncio.LimitOverrunError as exc:
        raise BrowserPolicyError("response headers exceed bound") from exc
    if len(value) > limit:
        raise BrowserPolicyError("response headers exceed bound")
    return value


def _parse_headers(value: bytes) -> tuple[int, tuple[tuple[str, str], ...]]:
    try:
        lines = value.decode("iso-8859-1").split("\r\n")
        version, status_text, _ = lines[0].split(" ", 2)
        status = int(status_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BrowserPolicyError("malformed HTTP status") from exc
    if version not in {"HTTP/1.0", "HTTP/1.1"} or not 100 <= status <= 599:
        raise BrowserPolicyError("unsupported HTTP response")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[:1].isspace() or ":" not in line:
            raise BrowserPolicyError("malformed HTTP header")
        name, raw = line.split(":", 1)
        headers.append((name.strip().lower(), raw.strip()))
    return status, tuple(headers)


def _header_values(headers, name):
    return tuple(value for key, value in headers if key == name)


async def _read_body(
    reader,
    headers,
    limit,
    timeout,
    cancellation,
    *,
    content_encoding_policy: str = "identity-only-v1",
) -> bytes:
    encodings = _header_values(headers, "content-encoding")
    if content_encoding_policy != "identity-only-v1":
        raise BrowserPolicyError("content encoding policy is unsupported")
    if encodings and any(value.lower() not in {"", "identity"} for value in encodings):
        raise BrowserPolicyError("content encoding is unsupported")
    transfers = _header_values(headers, "transfer-encoding")
    lengths = _header_values(headers, "content-length")
    if transfers:
        if lengths or transfers != ("chunked",):
            raise BrowserPolicyError("transfer encoding is ambiguous")
        return await _read_chunked(reader, limit, timeout, cancellation)
    if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
        raise BrowserPolicyError("content length is ambiguous")
    if lengths:
        length = int(lengths[0])
        if length > limit:
            raise BrowserPolicyError("response body exceeds bound")
        return await _await_with_cancel(
            reader.readexactly(length), cancellation, timeout
        )
    body = bytearray()
    while True:
        chunk = await _await_with_cancel(
            reader.read(min(65_536, limit - len(body) + 1)), cancellation, timeout
        )
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > limit:
            raise BrowserPolicyError("response body exceeds bound")


async def _read_chunked(reader, limit, timeout, cancellation):
    body = bytearray()
    while True:
        line = await _await_with_cancel(reader.readline(), cancellation, timeout)
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise BrowserPolicyError("malformed chunk size") from exc
        if size == 0:
            trailer = await _await_with_cancel(reader.readline(), cancellation, timeout)
            if trailer != b"\r\n":
                raise BrowserPolicyError("HTTP trailers are unsupported")
            return bytes(body)
        if len(body) + size > limit:
            raise BrowserPolicyError("response body exceeds bound")
        chunk = await _await_with_cancel(
            reader.readexactly(size + 2), cancellation, timeout
        )
        if not chunk.endswith(b"\r\n"):
            raise BrowserPolicyError("malformed chunk framing")
        body.extend(chunk[:-2])


class TrafilaturaSubprocessExtractor:
    async def extract(self, content: bytes, **values: object) -> tuple[str, str]:
        cancellation = values["cancellation"]
        request = (
            json.dumps(
                {
                    "media_type": values["media_type"],
                    "charset": values["charset"],
                    "max_characters": values["max_characters"],
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
            + content
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(Path(__file__).with_name("trafilatura_worker.py")),
            env=_minimal_environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        operation = asyncio.create_task(process.communicate(request))
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, cancelled},
                timeout=int(values["timeout_milliseconds"]) / 1_000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                raise asyncio.CancelledError
            if operation not in done:
                raise TimeoutError
            stdout, stderr = operation.result()
            if process.returncode or stderr or len(stdout) > 1_000_000:
                raise BrowserPolicyError("document extraction failed")
            parsed = json.loads(stdout)
            title, text = parsed["title"], parsed["text"]
            if not isinstance(title, str) or not isinstance(text, str) or not text:
                raise BrowserPolicyError("document extraction returned invalid data")
            return title, text
        finally:
            if process.returncode is None:
                process.kill()
            if not operation.done():
                operation.cancel()
            if not cancelled.done():
                cancelled.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await operation
            with suppress(Exception, asyncio.CancelledError):
                await cancelled
            with suppress(Exception, asyncio.CancelledError):
                await process.wait()


def _minimal_environment() -> dict[str, str]:
    value = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    for name in ("SYSTEMROOT", "WINDIR"):
        if name in os.environ:
            value[name] = os.environ[name]
    return value


class BoundedBrowserTool:
    def __init__(
        self,
        *,
        bound: BoundRealCapability,
        authorizer: RealToolDispatchAuthorizer,
        transport: BrowserHttpTransport,
        extractor: BrowserDocumentExtractor,
        clock: Clock,
    ) -> None:
        if bound.settings.browser_policy is None:
            raise ValueError("Browser Tool requires Browser policy")
        self._bound = bound
        self._authorizer = authorizer
        self._transport = transport
        self._extractor = extractor
        self._clock = clock

    @property
    def descriptor(self):
        return self._bound.settings.descriptor()

    @property
    def provider_call_reservation(self):
        return self._bound.settings.provider_reservation

    @property
    def delegates_retry_to_phase3(self) -> bool:
        return True

    async def invoke_authorized(self, envelope, cancellation):
        started = monotonic()
        policy = self._bound.settings.browser_policy
        assert policy is not None
        try:
            async with asyncio.timeout(policy.total_invocation_timeout_ms / 1_000):
                return await self._invoke_with_policy(
                    envelope, cancellation, started, policy
                )
        except TimeoutError:
            return _browser_failure("browser_total_timeout", True, started, True)

    async def _invoke_with_policy(self, envelope, cancellation, started, policy):
        request = envelope.invocation.input
        if not isinstance(request, BrowserRequest):
            return _browser_failure("browser_input_invalid", False, started, False)
        try:
            current = canonicalize_browser_url(
                request.url,
                allowed_schemes=policy.allowed_schemes,
                allowed_ports=policy.allowed_ports,
                max_characters=policy.max_url_characters,
                max_bytes=policy.max_url_bytes,
            )
        except BrowserPolicyError:
            return _browser_failure("browser_url_rejected", False, started, False)
        visited: set[str] = set()
        for _ in range(policy.max_redirects + 1):
            if current in visited:
                return _browser_failure("browser_redirect_loop", False, started, True)
            visited.add(current)
            try:
                self._authorizer.authorize(envelope)
            except Exception:
                return _browser_failure(
                    "real_tool_dispatch_unauthorized", False, started, False
                )
            try:
                response = await self._transport.get(
                    url=current,
                    max_header_bytes=policy.max_header_bytes,
                    max_body_bytes=policy.max_body_bytes,
                    connect_timeout_milliseconds=policy.connect_timeout_ms,
                read_timeout_milliseconds=policy.read_timeout_ms,
                content_encoding_policy=policy.content_encoding_policy,
                user_agent=policy.user_agent,
                    cancellation=cancellation,
                )
            except asyncio.CancelledError:
                if not cancellation.cancelled:
                    raise
                return ToolInvocationResult(
                    status=ToolInvocationStatus.CANCELLED,
                    error=ToolError(
                        code="browser_cancelled", message="Browser cancelled"
                    ),
                    usage=None,
                    usage_certainty=UsageCertainty.UNKNOWN,
                )
            except BrowserTransportError as exc:
                return _browser_failure(
                    exc.code, exc.retryable, started, exc.dispatched
                )
            if response.status_code in {301, 302, 303, 307, 308}:
                locations = _header_values(response.headers, "location")
                if len(locations) != 1:
                    return _browser_failure(
                        "browser_redirect_invalid", False, started, True
                    )
                if len(visited) > policy.max_redirects:
                    return _browser_failure(
                        "browser_redirect_limit", False, started, True
                    )
                try:
                    current = canonicalize_browser_url(
                        urljoin(current, locations[0]),
                        allowed_schemes=policy.allowed_schemes,
                        allowed_ports=policy.allowed_ports,
                        max_characters=policy.max_url_characters,
                        max_bytes=policy.max_url_bytes,
                    )
                except BrowserPolicyError:
                    return _browser_failure(
                        "browser_redirect_invalid", False, started, True
                    )
                continue
            if response.status_code != 200:
                retryable = (
                    response.status_code in {408, 429} or response.status_code >= 500
                )
                return _browser_failure(
                    f"browser_http_{response.status_code}", retryable, started, True
                )
            try:
                media_type, charset = _content_type(
                    response.headers,
                    mime_allowlist=policy.mime_allowlist,
                    charset_allowlist=policy.charset_allowlist,
                    content_encoding_policy=policy.content_encoding_policy,
                )
                title, content = await self._extractor.extract(
                    response.body,
                    media_type=media_type,
                    charset=charset,
                    timeout_milliseconds=policy.extraction_timeout_ms,
                    max_characters=policy.max_extracted_characters,
                    cancellation=cancellation,
                )
                normalized = normalize_browser_text(content)
                if not normalized or len(normalized) > policy.max_extracted_characters:
                    raise BrowserPolicyError("extracted content violates bound")
                return ToolInvocationResult(
                    status=ToolInvocationStatus.SUCCEEDED,
                    output=BrowserResult(
                        adapter_id=self.descriptor.adapter_id,
                        final_url=current,
                        title=title[:1_000],
                        content=normalized,
                        content_hash=sha256_text(normalized),
                        retrieved_at=self._clock.now(),
                        provenance={
                            "transport": "pinned-http-1.1",
                            "extraction_policy_version": (
                                policy.extraction_policy_version
                            ),
                            "content_media_type": media_type,
                        },
                    ),
                    usage=_browser_usage(started, 1),
                    usage_certainty=UsageCertainty.EXACT,
                )
            except asyncio.CancelledError:
                if not cancellation.cancelled:
                    raise
                return ToolInvocationResult(
                    status=ToolInvocationStatus.CANCELLED,
                    error=ToolError(
                        code="browser_cancelled", message="Browser cancelled"
                    ),
                    usage=None,
                    usage_certainty=UsageCertainty.UNKNOWN,
                )
            except (
                BrowserPolicyError,
                TimeoutError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
            ):
                return _browser_failure(
                    "browser_extraction_failed", False, started, True
                )
        return _browser_failure("browser_redirect_limit", False, started, True)


def _content_type(
    headers,
    *,
    mime_allowlist: tuple[str, ...] = BROWSER_ALLOWED_MIME_TYPES,
    charset_allowlist: tuple[str, ...] = BROWSER_ALLOWED_CHARSETS,
    content_encoding_policy: str = "identity-only-v1",
):
    if content_encoding_policy != "identity-only-v1":
        raise BrowserPolicyError("content encoding policy is unsupported")
    values = _header_values(headers, "content-type")
    if len(values) != 1:
        raise BrowserPolicyError("content type is missing or ambiguous")
    parts = [part.strip() for part in values[0].split(";")]
    media_type = parts[0].lower()
    if media_type not in mime_allowlist:
        raise BrowserPolicyError("content type is unsupported")
    charset = "utf-8"
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip('"').lower()
    if charset not in charset_allowlist:
        raise BrowserPolicyError("charset is unsupported")
    return media_type, charset


def normalize_browser_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _browser_usage(started, calls):
    return ToolUsage(
        duration_milliseconds=max(0, int((monotonic() - started) * 1_000)),
        tool_calls=calls,
    )


def _browser_failure(code, retryable, started, dispatched):
    return ToolInvocationResult(
        status=ToolInvocationStatus.FAILED,
        error=ToolError(
            code=code, message="Bounded Browser failed", retryable=retryable
        ),
        usage=_browser_usage(started, 1),
        usage_certainty=UsageCertainty.EXACT,
    )
