"""Public, anonymous, read-only acquisition for automatically authored recipes."""

import ipaddress
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import httpx

from tender_system.core.domain import ExecutionLimits
from tender_system.core.execution import (
    RequestBudgetExceeded,
    ResponseBudgetExceeded,
    TransportUsage,
)
from tender_system.core.ports import HttpAdapter, HttpRequest, HttpResponse

_PUBLIC_BUDGET: ContextVar[tuple[ExecutionLimits, TransportUsage] | None] = ContextVar(
    "public_acquisition", default=None
)


@contextmanager
def public_acquisition(limits: ExecutionLimits) -> Iterator[TransportUsage]:
    usage = TransportUsage()
    token = _PUBLIC_BUDGET.set((limits, usage))
    try:
        yield usage
    finally:
        _PUBLIC_BUDGET.reset(token)


def charge_public_request() -> None:
    state = _PUBLIC_BUDGET.get()
    if state:
        limits, usage = state
        if usage.requests >= limits.max_requests:
            raise RequestBudgetExceeded(limits.max_requests, usage.requests)
        usage.requests += 1


def charge_public_bytes(size: int) -> None:
    state = _PUBLIC_BUDGET.get()
    if state:
        limits, usage = state
        usage.response_bytes += size
        if usage.response_bytes > limits.max_total_response_bytes:
            raise ResponseBudgetExceeded(limits.max_total_response_bytes, usage.response_bytes)


def public_bytes_remaining(default: int) -> int:
    state = _PUBLIC_BUDGET.get()
    return (
        min(default, state[0].max_total_response_bytes - state[1].response_bytes)
        if state
        else default
    )


class UnsafeWebsite(ValueError):
    code = "unsupported_website"
    stage = "investigation"
    evidence_references: tuple[str, ...] = ()


def public_addresses(url: str) -> tuple[str, ...]:
    from tender_system.operator.operator_contracts import SourceURL

    SourceURL(url=url)
    parsed = urlsplit(url)
    if parsed.port not in {None, 80, 443}:
        raise UnsafeWebsite("Only public HTTP(S) websites on standard ports are supported")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise UnsafeWebsite("Website hostname could not be resolved") from None
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise UnsafeWebsite("Private, local and reserved network destinations are unsupported")
    return tuple(dict.fromkeys(str(row[4][0]) for row in addresses))


def public_url(url: str) -> None:
    public_addresses(url)


class PublicHTTPTransport(httpx.BaseTransport):
    """Connect to the validated IP; retain the original HTTP authority and TLS identity."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = (
            transport if transport is not None else httpx.HTTPTransport(trust_env=False)
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        address = public_addresses(str(request.url))[0]
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return self.transport.handle_request(pinned)

    def close(self) -> None:
        self.transport.close()


class PublicReadOnlyHttp:
    """Delegate must disable automatic redirects; validate every hop before sending."""

    def __init__(self, http: HttpAdapter, check: Callable[[str], None] = public_url):
        self.http, self.check = http, check

    def request(self, request: HttpRequest) -> HttpResponse:
        if request.method.upper() not in {"GET", "HEAD"} or request.body is not None:
            raise UnsafeWebsite("Automatic recipes support anonymous GET/HEAD requests only")
        requests = size = 0
        for _ in range(min(10, request.max_requests)):
            self.check(request.url)
            available = public_bytes_remaining(request.max_response_bytes - size)
            if available < 1:
                raise ResponseBudgetExceeded(request.max_response_bytes, size)
            charge_public_request()
            try:
                response = self.http.request(
                    replace(
                        request,
                        max_requests=1,
                        max_response_bytes=available,
                    )
                )
            except Exception as error:
                charge_public_bytes(getattr(error, "bytes_received", 0))
                raise
            charge_public_bytes(
                response.bytes_received
                if response.bytes_received is not None
                else len(response.body)
            )
            requests += response.requests_made
            size += (
                response.bytes_received
                if response.bytes_received is not None
                else len(response.body)
            )
            if httpx.URL(response.url) != httpx.URL(request.url):
                # A custom adapter must not hide redirect hops from policy checks.
                self.check(response.url)
                raise UnsafeWebsite("Acquisition adapter followed an unchecked redirect")
            location = response.headers.get("location")
            if 300 <= response.status_code < 400 and location:
                request = replace(request, url=urljoin(request.url, location), headers={})
                continue
            if response.status_code in {401, 403, 429}:
                raise UnsafeWebsite("Website requires access authorization or rate-limit recovery")
            return replace(response, requests_made=requests, bytes_received=size)
        raise UnsafeWebsite("Website redirect/request limit reached")
