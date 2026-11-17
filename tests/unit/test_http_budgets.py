"""Enforce byte limits while receiving, before retaining source response evidence."""

import gzip
from collections.abc import Generator, Iterator
from uuid import uuid4

import httpx
import pytest

from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore
from tender_system.collection.recipes import RecipeExecutor
from tender_system.collection.validation_evidence import (
    CapturedResponseHttpAdapter,
    request_fingerprint,
)
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    ExecutionContext,
    ExecutionLimits,
    SourceFailure,
    SourceRecipe,
)
from tender_system.core.execution import RedirectLimitExceeded, RequestBudgetExceeded
from tender_system.core.ports import HttpRequest, HttpResponse
from tender_system.testing.fakes import FunctionHttpAdapter


def test_streaming_transport_stops_oversized_response_and_closes_stream() -> None:
    class Stream(httpx.SyncByteStream):
        consumed = 0
        closed = False

        def __iter__(self) -> Iterator[bytes]:
            for _ in range(100):
                self.consumed += 1
                yield b"x" * 1000

        def close(self) -> None:
            self.closed = True

    stream = Stream()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(ValueError, match="response byte budget exceeded"):
            HttpxAdapter(client).request(
                HttpRequest("http://website.test/large", max_response_bytes=1500)
            )
    assert stream.consumed == 2 and stream.closed


@pytest.mark.parametrize("per_response,total", [(100, 1000), (150, 250)])
def test_executor_checks_replacement_transport_and_total_byte_budgets(
    per_response: int, total: int
) -> None:
    evidence = MemoryEvidenceStore()
    calls = 0

    def respond(request: HttpRequest) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse(request.url, 200, b"x" * 120)

    recipe = SourceRecipe.model_validate(
        {
            "source_id": "bytes",
            "version": "1",
            "steps": [
                {"type": "http_request", "parameters": {"url": f"http://website.test/{i}"}}
                for i in range(10)
            ],
        }
    )
    result = RecipeExecutor(FunctionHttpAdapter(respond), evidence, SourceSettings()).execute(
        recipe,
        ExecutionContext(
            run_id=uuid4(),
            limits=ExecutionLimits(max_response_bytes=per_response, max_total_response_bytes=total),
        ),
    )
    assert isinstance(result, SourceFailure)
    assert result.error_code == "response_budget_exceeded"
    assert "byte budget exceeded" in result.error_detail
    assert calls == (1 if per_response == 100 else 3)
    captured = evidence.get(result.failed_evidence_reference)
    exchanges = captured["http_exchanges"]
    assert isinstance(exchanges, list)
    assert all(
        isinstance(item, dict)
        and (
            "body_base64" not in item
            or (isinstance(item["body_base64"], str) and len(item["body_base64"]) <= 200)
        )
        for item in exchanges
    )


def test_redirect_hops_count_against_the_real_request_budget() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path != "/3":
            return httpx.Response(302, headers={"Location": f"/{int(request.url.path[1:]) + 1}"})
        return httpx.Response(200, json={"ok": True})

    recipe = SourceRecipe.model_validate(
        {
            "source_id": "redirect",
            "version": "1",
            "steps": [{"type": "http_request", "parameters": {"url": "http://website.test/1"}}],
        }
    )
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        result = RecipeExecutor(
            HttpxAdapter(client), MemoryEvidenceStore(), SourceSettings()
        ).execute(recipe, ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_requests=2)))
    assert isinstance(result, SourceFailure)
    assert result.error_code == "request_budget_exceeded"
    assert requested == ["/1", "/2"]
    assert result.diagnostics.transport_attempts == 2


def test_redirect_success_and_offline_replay_preserve_physical_accounting() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path != "/3":
            return httpx.Response(
                302,
                headers={"Location": f"/{int(request.url.path[1:]) + 1}"},
                content=b"redirect",
            )
        return httpx.Response(200, json={"external_tender_id": "1", "title": "Equipment"})

    recipe = SourceRecipe.model_validate(
        {
            "source_id": "redirect",
            "version": "1",
            "steps": [
                {"type": "http_request", "parameters": {"url": "http://website.test/1"}},
                {
                    "type": "extract",
                    "parameters": {
                        "input": {"$ref": "response.body"},
                        "format": "json",
                        "emit": True,
                    },
                },
            ],
        }
    )
    evidence = MemoryEvidenceStore()
    limits = ExecutionLimits(max_requests=3)
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        result = RecipeExecutor(HttpxAdapter(client), evidence, SourceSettings()).execute(
            recipe, ExecutionContext(run_id=uuid4(), limits=limits)
        )
    assert isinstance(result, CollectionRun)
    assert requested == ["/1", "/2", "/3"]
    assert result.diagnostics.transport_attempts == 3
    assert result.diagnostics.response_bytes > len(b"redirect") * 2
    captured = evidence.get(result.evidence_reference)
    replay = CapturedResponseHttpAdapter(captured["http_exchanges"])
    repeated = RecipeExecutor(replay, evidence, SourceSettings()).execute(
        recipe, ExecutionContext(run_id=uuid4(), limits=limits)
    )
    assert isinstance(repeated, CollectionRun)
    assert repeated.raw_observations == result.raw_observations
    assert repeated.diagnostics.transport_attempts == result.diagnostics.transport_attempts
    assert repeated.diagnostics.response_bytes == result.diagnostics.response_bytes
    replay.assert_exhausted()
    assert requested == ["/1", "/2", "/3"]


def test_redirect_does_not_reapply_client_auth_to_another_host() -> None:
    seen: list[tuple[str, str | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("Authorization")))
        if request.url.host == "origin.test":
            return httpx.Response(302, headers={"Location": "http://other.test/final"})
        return httpx.Response(200, content=b"done")

    with httpx.Client(
        transport=httpx.MockTransport(respond),
        follow_redirects=True,
        auth=("fixture", "credential"),
    ) as client:
        response = HttpxAdapter(client).request(HttpRequest("http://origin.test/start"))
    assert response.requests_made == 2
    assert seen[0][0] == "origin.test" and seen[0][1] is not None
    assert seen[1] == ("other.test", None)


@pytest.mark.parametrize("status,method,body", [(303, "GET", b""), (307, "POST", b"payload")])
def test_redirect_method_and_body_follow_standard_httpx_contract(
    status: int, method: str, body: bytes
) -> None:
    seen: list[tuple[str, bytes]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.content))
        if request.url.path == "/start":
            return httpx.Response(status, headers={"Location": "/final"})
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        response = HttpxAdapter(client).request(
            HttpRequest("http://website.test/start", method="POST", body=b"payload")
        )
    assert response.requests_made == 2
    assert seen == [("POST", b"payload"), (method, body)]


def test_redirect_loop_stops_at_client_cap_and_closes_every_response() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/again"})

    with httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=True, max_redirects=2
    ) as client:
        with pytest.raises(RedirectLimitExceeded) as caught:
            HttpxAdapter(client).request(HttpRequest("http://website.test/start", max_requests=10))
    assert caught.value.requests_made == 3
    assert requested == ["/start", "/again", "/again"]


def test_explicit_302_handling_honors_injected_client_redirect_configuration() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/must-not-follow"})

    recipe = SourceRecipe.model_validate(
        {
            "source_id": "manual-redirect",
            "version": "1",
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {
                        "url": "http://website.test/start",
                        "accepted_statuses": [302],
                    },
                },
            ],
        }
    )
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False) as client:
        result = RecipeExecutor(
            HttpxAdapter(client), MemoryEvidenceStore(), SourceSettings()
        ).execute(recipe, ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_requests=1)))
    assert isinstance(result, CollectionRun)
    assert result.diagnostics.transport_attempts == 1 and requested == ["/start"]


@pytest.mark.parametrize("failure", ["timeout", "connection"])
def test_redirect_retry_accounts_failed_hops_and_cannot_exceed_run_budget(failure: str) -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/failing"}, content=b"redirect")
        if failure == "timeout":
            raise httpx.ReadTimeout("private transport detail", request=request)
        raise httpx.ConnectError("private transport detail", request=request)

    recipe = SourceRecipe.model_validate(
        {
            "source_id": "redirect-failure",
            "version": "1",
            "steps": [
                {"type": "http_request", "parameters": {"url": "http://website.test/start"}},
            ],
        }
    )
    settings = SourceSettings.model_validate(
        {"retry": {"max_attempts": 2, "backoff_seconds": 0.001}}
    )
    evidence = MemoryEvidenceStore()
    limits = ExecutionLimits(max_requests=3)
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        result = RecipeExecutor(HttpxAdapter(client), evidence, settings).execute(
            recipe, ExecutionContext(run_id=uuid4(), limits=limits)
        )
    assert isinstance(result, SourceFailure) and result.error_code == "request_budget_exceeded"
    assert result.diagnostics.transport_attempts == 3
    assert result.diagnostics.response_bytes == 2 * len(b"redirect")
    assert requested == ["/start", "/failing", "/start"]
    assert "private" not in result.error_detail
    replay = CapturedResponseHttpAdapter(
        evidence.get(result.failed_evidence_reference)["http_exchanges"]
    )
    repeated = RecipeExecutor(replay, evidence, settings).execute(
        recipe, ExecutionContext(run_id=uuid4(), limits=limits)
    )
    assert isinstance(repeated, SourceFailure) and repeated.error_code == result.error_code
    assert repeated.diagnostics.transport_attempts == 3
    assert repeated.diagnostics.response_bytes == result.diagnostics.response_bytes
    replay.assert_exhausted()


def test_redirect_bytes_consume_the_same_response_budget() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/again"}, content=b"x" * 100)

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(ValueError, match="response byte budget exceeded") as caught:
            HttpxAdapter(client).request(
                HttpRequest("http://website.test/start", max_response_bytes=150)
            )
    assert getattr(caught.value, "requests_made", None) == 2
    assert getattr(caught.value, "bytes_received", None) == 200
    assert requested == ["/start", "/again"]


def test_operational_budgets_do_not_change_request_fingerprint() -> None:
    original = HttpRequest("http://website.test/start")
    bounded = HttpRequest(original.url, timeout_seconds=1, max_response_bytes=5, max_requests=1)
    assert request_fingerprint(original) == request_fingerprint(bounded)


@pytest.mark.parametrize("requests_made,bytes_received", [(True, 2), (0, 2), (1, 1), (1, -1)])
def test_replay_rejects_invalid_physical_accounting(
    requests_made: int, bytes_received: int
) -> None:
    request = HttpRequest("http://website.test/start")
    replay = CapturedResponseHttpAdapter(
        [
            {
                "request_fingerprint": request_fingerprint(request),
                "status_code": 200,
                "body_base64": "e30=",
                "requests_made": requests_made,
                "bytes_received": bytes_received,
            }
        ]
    )
    with pytest.raises(ValueError, match="accounting"):
        replay.request(request)


def test_physical_request_budget_error_exposes_safe_accounting() -> None:
    error = RequestBudgetExceeded(2, 2, 100)
    assert error.code == "request_budget_exceeded" and error.stage == "http_request"
    assert error.requests_made == 2 and error.bytes_received == 100


@pytest.mark.parametrize("budget", [1, 2])
def test_digest_auth_challenge_requests_are_budgeted_before_dispatch(budget: int) -> None:
    requested: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization")
        requested.append(authorization)
        if authorization is None:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Digest realm="fixture", nonce="123", qop="auth"'},
                content=b"challenge",
            )
        return httpx.Response(200, content=b"accepted")

    with httpx.Client(
        transport=httpx.MockTransport(respond), auth=httpx.DigestAuth("fixture", "credential")
    ) as client:
        if budget == 1:
            with pytest.raises(RequestBudgetExceeded) as caught:
                HttpxAdapter(client).request(
                    HttpRequest("http://website.test", max_requests=budget)
                )
            assert caught.value.requests_made == 1 and requested == [None]
        else:
            response = HttpxAdapter(client).request(
                HttpRequest("http://website.test", max_requests=budget)
            )
            assert response.requests_made == 2
            assert response.bytes_received == len(b"challengeaccepted")
            assert len(requested) == 2 and requested[1] is not None


def test_auth_body_verification_reads_captured_decoded_bytes_once() -> None:
    class BodyAuth(httpx.Auth):
        requires_response_body = True

        def auth_flow(
            self, request: httpx.Request
        ) -> Generator[httpx.Request, httpx.Response, None]:
            response = yield request
            assert response.content == b"decoded content"

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=gzip.compress(b"decoded content"), headers={"Content-Encoding": "gzip"}
        )

    with httpx.Client(transport=httpx.MockTransport(respond), auth=BodyAuth()) as client:
        response = HttpxAdapter(client).request(HttpRequest("http://website.test"))
    assert response.body == b"decoded content" and response.requests_made == 1


def test_http_deadline_prevents_sending_another_redirect_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    requested: list[str] = []
    monkeypatch.setattr("tender_system.collection.adapters.time.monotonic", lambda: clock[0])

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        clock[0] = 1.1
        return httpx.Response(302, headers={"Location": "/must-not-send"})

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(TimeoutError) as caught:
            HttpxAdapter(client).request(
                HttpRequest("http://website.test/start", timeout_seconds=1)
            )
    assert requested == ["/start"]
    assert getattr(caught.value, "requests_made", None) == 1


@pytest.mark.parametrize("requests_made,bytes_received", [(0, 100), (1, 5)])
def test_replay_rejects_failure_byte_accounting_drift(
    requests_made: int, bytes_received: int
) -> None:
    request = HttpRequest("http://website.test")
    replay = CapturedResponseHttpAdapter(
        [
            {
                "request_fingerprint": request_fingerprint(request),
                "error": "ResponseBudgetExceeded",
                "limit": 50,
                "received": 100,
                "requests_made": requests_made,
                "bytes_received": bytes_received,
            }
        ]
    )
    with pytest.raises(ValueError, match="accounting"):
        replay.request(request)
