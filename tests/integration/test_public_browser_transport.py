"""Chromium redirects and JS fetches use the pinned HTTP transport."""

import socket
from typing import Any

import httpx
import pytest

from tender_system.collection.adapters import HttpxAdapter, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.collection.network import PublicHTTPTransport, UnsafeWebsite, public_url

pytestmark = pytest.mark.browser


def test_public_browser_routes_redirects_assets_and_javascript_through_pinned_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str]] = []
    original_resolve = socket.getaddrinfo

    def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(host, str) and host.endswith("example.org"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]
        return original_resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    def send(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.215.14"
        host = request.headers["host"]
        assert request.extensions["sni_hostname"] == host
        sent.append((host, request.url.path))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://other.example.org/page"})
        if request.url.path == "/api":
            return httpx.Response(200, json={"title": "Pinned public tender"})
        if request.url.path == "/style.css":
            return httpx.Response(
                200, text="body { color: black; }", headers={"content-type": "text/css"}
            )
        return httpx.Response(
            200,
            text="""<html><head><link rel="stylesheet" href="/style.css">
            </head><body><h1>Loading</h1><script>fetch('/api').then(r=>r.json()).then(x=>{
            document.querySelector('h1').textContent=x.title; });</script></body></html>""",
            headers={"content-type": "text/html"},
        )

    with httpx.Client(transport=PublicHTTPTransport(httpx.MockTransport(send))) as client:
        browser = PlaywrightBrowserAdapter(
            MemorySessionStore(),
            timeout_seconds=5,
            request_guard=lambda url, method: public_url(url),
            request_http=HttpxAdapter(client),
        )
        try:
            browser.navigate("https://example.org/start")
            browser.wait_for_selector('h1:has-text("Pinned public tender")')
            assert browser.current_url() == "https://other.example.org/page"
            assert "Pinned public tender" in browser.content()
        finally:
            browser.close()
    assert ("example.org", "/start") in sent
    assert ("other.example.org", "/page") in sent
    assert ("other.example.org", "/api") in sent
    assert ("other.example.org", "/style.css") in sent


def test_public_browser_rejects_rebinding_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = 0
    original_resolve = socket.getaddrinfo

    def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal resolutions
        if host == "example.org":
            resolutions += 1
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.215.14" if resolutions == 1 else "127.0.0.1", port),
                )
            ]
        return original_resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with httpx.Client(
        transport=PublicHTTPTransport(
            httpx.MockTransport(lambda _: pytest.fail("private address must not reach transport"))
        )
    ) as client:
        browser = PlaywrightBrowserAdapter(
            MemorySessionStore(),
            timeout_seconds=5,
            request_guard=lambda url, method: public_url(url),
            request_http=HttpxAdapter(client),
        )
        try:
            with pytest.raises(UnsafeWebsite, match="Private, local"):
                browser.navigate("https://example.org/start")
        finally:
            browser.close()
    assert resolutions == 2


def test_blocked_background_requests_do_not_poison_public_document() -> None:
    sent: list[str] = []

    def guard(url: str, method: str) -> None:
        if ":8443" in url or method not in {"GET", "HEAD"}:
            raise UnsafeWebsite("Background resource denied")

    def send(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        assert request.method == "GET" and request.url.port != 8443
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="""
            <html><body><h1>Public tender</h1>
            <script src="https://chat.example.org:8443/widget.js"></script>
            <script>fetch('/analytics', {method:'POST'}).catch(() => {
                document.body.setAttribute('data-blocked', 'yes');
            });</script></body></html>""",
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        browser = PlaywrightBrowserAdapter(
            MemorySessionStore(),
            timeout_seconds=5,
            request_guard=guard,
            request_http=HttpxAdapter(client),
        )
        try:
            browser.navigate("https://example.org/tenders")
            browser.wait_for_selector('body[data-blocked="yes"]')
            assert "Public tender" in browser.content()
        finally:
            browser.close()
    assert sent == ["https://example.org/tenders"]
