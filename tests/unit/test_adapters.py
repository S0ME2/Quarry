from datetime import UTC, datetime

import pytest

from tender_system.core.ports import (
    BrowserAdapter,
    Clock,
    HttpAdapter,
    HttpRequest,
    HttpResponse,
    ModelClient,
    ModelRequest,
    ModelResponse,
    NetworkExchange,
)
from tender_system.testing.fakes import (
    FakeBrowserAdapter,
    FakeClock,
    FakePage,
    FunctionHttpAdapter,
    FunctionModelClient,
    ScriptedHttpAdapter,
    ScriptedModelClient,
)


@pytest.mark.parametrize("implementation", ["scripted", "function"])
def test_http_implementations_obey_same_port(implementation: str) -> None:
    response = HttpResponse(url="https://fixture.test/api", status_code=200, body=b'{"items": []}')
    adapter: HttpAdapter
    if implementation == "scripted":
        adapter = ScriptedHttpAdapter((response,))
    else:
        adapter = FunctionHttpAdapter(lambda request: response)
    request = HttpRequest(url=response.url, headers={"X-Test": "yes"}, timeout_seconds=4)
    assert adapter.request(request) == response


def test_http_failures_and_exhaustion_are_explicit() -> None:
    adapter = ScriptedHttpAdapter((TimeoutError("fixture timeout"),))
    request = HttpRequest(url="https://fixture.test")
    with pytest.raises(TimeoutError, match="fixture timeout"):
        adapter.request(request)
    with pytest.raises(AssertionError, match="script exhausted"):
        adapter.request(request)
    assert adapter.requests == [request, request]


def test_http_payloads_are_isolated_from_fixture_and_request_mutation() -> None:
    headers = {"X-Test": "original"}
    adapter = ScriptedHttpAdapter((HttpResponse(url="fixture", status_code=200, body=b"data"),))
    adapter.request(HttpRequest(url="fixture", headers=headers))
    headers["X-Test"] = "changed"
    assert adapter.requests[0].headers["X-Test"] == "original"


@pytest.mark.parametrize("implementation", ["scripted", "function"])
def test_model_implementations_obey_same_port(implementation: str) -> None:
    response = ModelResponse(output={"result": "unsure"}, model_version="fake-v1", input_tokens=5)
    adapter: ModelClient
    if implementation == "scripted":
        adapter = ScriptedModelClient((response,))
    else:
        adapter = FunctionModelClient(lambda request: response)
    request = ModelRequest(model="fixture-model", task="matching", payload={"title": "Tender"})
    assert adapter.generate(request) == response


def test_model_failures_malformed_data_and_exhaustion_remain_observable() -> None:
    malformed = ModelResponse(output={"result": "invalid"}, model_version="fake-v1")
    adapter = ScriptedModelClient((TimeoutError("fixture model timeout"), malformed))
    request = ModelRequest(model="fixture", task="fixing", payload={})
    with pytest.raises(TimeoutError):
        adapter.generate(request)
    # Adapter carries raw output; domain-specific parsing belongs to matcher/fixer later.
    assert adapter.generate(request) == malformed
    with pytest.raises(AssertionError):
        adapter.generate(request)


def test_model_response_does_not_leak_mutable_state_between_calls() -> None:
    output = {"text": "original"}
    response = ModelResponse(output=output, model_version="fake-v1")
    adapter = ScriptedModelClient((response, response))
    request = ModelRequest(model="fixture", task="matching", payload={})
    first = adapter.generate(request)
    output["text"] = "changed"
    assert first.output["text"] == "original"
    assert adapter.generate(request).output["text"] == "original"
    assert "changed" not in repr(response)


def test_browser_capabilities_and_session_lifecycle() -> None:
    exchange = NetworkExchange(
        request=HttpRequest(url="fixture/api"),
        response=HttpResponse(url="fixture/api", status_code=200, body=b"[]"),
    )
    fake = FakeBrowserAdapter(
        {
            "fixture/login": FakePage(html="login", click_targets={"#submit": "fixture/list"}),
            "fixture/list": FakePage(
                html="<h1>Tenders</h1>",
                network_exchanges=(exchange,),
                downloads={"fixture/file": b"file"},
            ),
        }
    )
    browser: BrowserAdapter = fake
    browser.restore_session("authorized-secret-reference")
    browser.navigate("fixture/login")
    browser.type_text("#password", "test-secret-value")
    browser.click("#submit")
    browser.select("#filter", "open")
    browser.scroll(500)
    assert browser.content() == "<h1>Tenders</h1>"
    assert browser.network() == (exchange,)
    assert browser.screenshot() == b"fake-screenshot"
    assert browser.trace() == b"fake-trace"
    assert browser.download("fixture/file") == b"file"
    assert "test-secret-value" not in repr(fake.events)
    assert "authorized-secret-reference" not in repr(fake.events)
    browser.close()
    browser.close()
    with pytest.raises(RuntimeError, match="closed"):
        browser.content()


def test_browser_sessions_are_isolated_and_failures_propagate() -> None:
    pages = {"fixture": FakePage(html="content")}
    first = FakeBrowserAdapter(pages)
    second = FakeBrowserAdapter(pages, {"navigate": TimeoutError("expired session")})
    first.navigate("fixture")
    with pytest.raises(TimeoutError, match="expired session"):
        second.navigate("fixture")
    assert first.content() == "content"
    assert second.events == []
    with pytest.raises(RuntimeError, match="navigate"):
        FakeBrowserAdapter(pages).content()
    with pytest.raises(LookupError, match="unknown fixture"):
        first.navigate("unknown")


def test_clock_is_injected_and_controllable() -> None:
    fake = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock: Clock = fake
    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)
    fake.instant = datetime(2026, 1, 2, tzinfo=UTC)
    assert clock.now() == fake.instant


def test_sensitive_transport_payloads_not_in_repr() -> None:
    secret = "test-secret-value"
    request = HttpRequest(url="fixture", headers={"Authorization": secret}, body=secret.encode())
    assert secret not in repr(request)
    assert secret not in repr(ModelRequest(model="fake", task="test", payload={"key": secret}))
