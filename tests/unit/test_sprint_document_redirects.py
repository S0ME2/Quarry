"""Attachment acquisition pins public DNS and rejects unsafe redirect targets."""

import socket
import time

import httpx
import pytest

from tender_system.collection.documents import DocumentFailure, DownloadBudget, download


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/private",
        "http://169.254.169.254/metadata",
        "https://user:password@public.example/file",
        "file:///etc/passwd",
    ],
)
def test_attachment_redirect_is_checked_before_another_request(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    requests: list[httpx.Request] = []
    original_client = httpx.Client

    def resolve(host: str, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        address = host if host in {"127.0.0.1", "169.254.169.254"} else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def send(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": target})

    def client(*, follow_redirects: bool, trust_env: bool) -> httpx.Client:
        assert follow_redirects is False and trust_env is False
        return original_client(
            transport=httpx.MockTransport(send),
            follow_redirects=follow_redirects,
            trust_env=trust_env,
        )

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx, "Client", client)
    budget = DownloadBudget(0, 0, 5, 1000, 1000, time.monotonic() + 10)
    with pytest.raises(DocumentFailure):
        download("https://public.example/file", {}, budget)
    assert len(requests) == budget.requests == 1
    # The transport receives the validated IP, preserving TLS SNI and HTTP identity.
    assert requests[0].url.host == "8.8.8.8"
    assert requests[0].headers["host"] == "public.example"
    assert requests[0].extensions["sni_hostname"] == "public.example"
