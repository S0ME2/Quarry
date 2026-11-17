"""Real Chromium must bound owned website subresources and captured bytes."""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler

import pytest

from tender_system.collection.adapters import MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter, UnsupportedBrowserTarget
from tender_system.core.domain import ExecutionLimits
from tender_system.core.execution import (
    RequestBudgetExceeded,
    ResponseBudgetExceeded,
    execution_budget,
)
from tender_system.testing.local_server import local_http_server, server_url

pytestmark = pytest.mark.browser


@pytest.fixture
def budget_site() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append(self.path)
            kind = "text/html"
            if self.path.startswith("/download-redirect/"):
                self.send_response(302)
                self.send_header("Location", "/medium/1")
                self.send_header("Content-Length", "700")
                self.end_headers()
                self.wfile.write(b"x" * 700)
                return
            if self.path.startswith("/redirect/") and int(self.path.rsplit("/", 1)[-1]) < 5:
                number = int(self.path.rsplit("/", 1)[-1]) + 1
                self.send_response(302)
                self.send_header("Location", f"/redirect/{number}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/redirect/5":
                body = b"<html><body>finished</body></html>"
            elif self.path == "/popup-page":
                body = b"<html><body><script>window.open('/requests')</script></body></html>"
            elif self.path == "/worker-page":
                body = (
                    b"<html><body><div id='ready'></div><script>let w=new Worker('/worker.js');"
                    b"w.onmessage=()=>document.querySelector('#ready').textContent='done';"
                    b"</script></body></html>"
                )
            elif self.path == "/worker.js":
                kind = "text/javascript"
                body = (
                    b"Promise.all(Array.from({length:12},(_,i)=>fetch('/medium/'+i)))"
                    b".then(()=>postMessage('done'))"
                )
            elif self.path == "/asset-page":
                body = (
                    "<html><head>"
                    + "".join(f'<link rel="stylesheet" href="/style/{i}">' for i in range(12))
                    + "</head><body>Assets</body></html>"
                ).encode()
            elif self.path.startswith("/style/"):
                kind = "text/css"
                body = b"/*" + b"x" * 696 + b"*/"
            elif self.path == "/requests":
                body = (
                    "<html><body>"
                    + "".join(f'<img src="/image/{i}">' for i in range(12))
                    + "</body></html>"
                ).encode()
            elif self.path in {"/large-page", "/aggregate-page", "/unknown-page"}:
                urls = {
                    "/large-page": ["/large"],
                    "/aggregate-page": ["/medium/1", "/medium/2", "/medium/3"],
                    "/unknown-page": ["/unknown"],
                }[self.path]
                body = (
                    "<html><body><div id='ready'></div><script>"
                    + "Promise.all(["
                    + ",".join(f"fetch('{u}').then(r=>r.text())" for u in urls)
                    + "]).then(()=>document.querySelector('#ready').textContent='done');"
                    + "</script></body></html>"
                ).encode()
            else:
                kind = "application/octet-stream"
                body = b"x" * (2048 if self.path in {"/large", "/unknown"} else 700)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            if self.path != "/unknown":
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with local_http_server(Handler) as server:
        yield server_url(server), requests


def test_browser_honors_request_budget_before_sending_extra_subresources(
    budget_site: tuple[str, list[str]],
) -> None:
    url, requests = budget_site
    with execution_budget(10, limits=ExecutionLimits(max_requests=4)):
        browser = PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=3)
        try:
            with pytest.raises(RequestBudgetExceeded):
                browser.navigate(url + "/requests")
            assert len(requests) <= 4
            with pytest.raises(RequestBudgetExceeded):
                browser.content()
            with pytest.raises(RequestBudgetExceeded):
                browser.network()
            for action in (
                lambda: browser.navigate(url),
                browser.current_url,
                lambda: browser.click("body"),
                lambda: browser.type_text("input", "ignored"),
                lambda: browser.select("select", "ignored"),
                lambda: browser.scroll(1),
                browser.screenshot,
                browser.trace,
                lambda: browser.download(url),
                lambda: browser.restore_session("unused"),
                browser.save_session,
                browser.session_state,
                lambda: browser.wait_for_selector("body"),
                lambda: browser.wait_for_response("*"),
            ):
                with pytest.raises(RequestBudgetExceeded):
                    action()
        finally:
            browser.close()


def test_browser_download_redirects_and_body_share_explicit_budgets(
    budget_site: tuple[str, list[str]],
) -> None:
    url, requests = budget_site
    browser = PlaywrightBrowserAdapter(
        MemorySessionStore(),
        timeout_seconds=3,
        limits=ExecutionLimits(max_requests=2),
    )
    try:
        with pytest.raises(RequestBudgetExceeded):
            browser.download(url + "/redirect/0")
        assert len(requests) == 2
    finally:
        browser.close()
    browser = PlaywrightBrowserAdapter(
        MemorySessionStore(),
        timeout_seconds=3,
        limits=ExecutionLimits(max_response_bytes=1024),
    )
    try:
        with pytest.raises(ResponseBudgetExceeded):
            browser.download(url + "/large")
    finally:
        browser.close()


def test_dedicated_worker_requests_share_the_browser_budget(
    budget_site: tuple[str, list[str]],
) -> None:
    url, requests = budget_site
    browser = PlaywrightBrowserAdapter(
        MemorySessionStore(),
        timeout_seconds=1,
        limits=ExecutionLimits(max_requests=4),
    )
    try:
        with pytest.raises(RequestBudgetExceeded):
            browser.navigate(url + "/worker-page")
            browser.wait_for_selector("#ready:has-text('done')")
        assert len(requests) <= 4
    finally:
        browser.close()


def test_popup_network_fails_closed_instead_of_escaping_capture_budget(
    budget_site: tuple[str, list[str]],
) -> None:
    url, requests = budget_site
    browser = PlaywrightBrowserAdapter(
        MemorySessionStore(),
        timeout_seconds=1,
        limits=ExecutionLimits(max_requests=4),
    )
    try:
        with pytest.raises(UnsupportedBrowserTarget, match="Popup acquisition"):
            browser.navigate(url + "/popup-page")
            browser.wait_for_selector("#never-created")
        assert requests == ["/popup-page"]
    finally:
        browser.close()


def test_browser_redirects_cannot_escape_request_budget(
    budget_site: tuple[str, list[str]],
) -> None:
    url, requests = budget_site
    with execution_budget(10, limits=ExecutionLimits(max_requests=2)):
        browser = PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=3)
        try:
            with pytest.raises(RequestBudgetExceeded):
                browser.navigate(url + "/redirect/0")
            assert len(requests) <= 2
        finally:
            browser.close()


@pytest.mark.parametrize("path", ["/large-page", "/unknown-page", "/aggregate-page"])
def test_browser_capture_rejects_oversized_known_unknown_and_aggregate_bodies(
    budget_site: tuple[str, list[str]],
    path: str,
) -> None:
    url, _ = budget_site
    limits = ExecutionLimits(
        max_requests=10, max_response_bytes=1024, max_total_response_bytes=1800
    )
    with execution_budget(10, limits=limits):
        browser = PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=3)
        try:
            with pytest.raises(ResponseBudgetExceeded):
                browser.navigate(url + path)
                browser.wait_for_selector("#ready:has-text('done')")
            with pytest.raises(ResponseBudgetExceeded):
                browser.network()
            with pytest.raises(ResponseBudgetExceeded):
                browser.current_url()
        finally:
            browser.close()


@pytest.mark.parametrize("browser_first", [False, True])
def test_mixed_recipe_shares_physical_request_budget(
    budget_site: tuple[str, list[str]],
    browser_first: bool,
) -> None:
    from uuid import uuid4

    from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore
    from tender_system.collection.recipes import RecipeExecutor
    from tender_system.core.configuration import SourceSettings
    from tender_system.core.domain import ExecutionContext, SourceFailure, SourceRecipe

    url, requests = budget_site
    http_step = {"type": "http_request", "parameters": {"url": url + "/medium/1"}}
    browser_step = {
        "type": "browser_action",
        "parameters": {"action": "navigate", "value": url + "/redirect/5"},
    }
    steps = (
        [browser_step, http_step, http_step]
        if browser_first
        else [http_step, http_step, browser_step]
    )
    limits = ExecutionLimits(max_requests=2)
    http = HttpxAdapter()
    try:
        result = RecipeExecutor(
            http,
            MemoryEvidenceStore(),
            SourceSettings(),
            browser_factory=lambda: PlaywrightBrowserAdapter(
                MemorySessionStore(), timeout_seconds=3
            ),
        ).execute(
            SourceRecipe.model_validate({"source_id": "mixed", "version": "1", "steps": steps}),
            ExecutionContext(run_id=uuid4(), limits=limits),
        )
    finally:
        http.close()
    assert isinstance(result, SourceFailure)
    assert result.error_code == "request_budget_exceeded"
    assert len(requests) == result.diagnostics.transport_attempts == 2
    assert result.diagnostics.browser_requests == int(browser_first)


def test_mixed_download_budget_and_repeated_browser_replay(
    budget_site: tuple[str, list[str]],
) -> None:
    from uuid import uuid4

    from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore
    from tender_system.collection.recipes import RecipeExecutor
    from tender_system.collection.validation_evidence import (
        CapturedBrowserSteps,
        CapturedResponseHttpAdapter,
    )
    from tender_system.core.configuration import SourceSettings
    from tender_system.core.domain import (
        CollectionRun,
        ExecutionContext,
        SourceFailure,
        SourceRecipe,
    )

    url, requests = budget_site
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "mixed",
            "version": "1",
            "steps": [
                {
                    "type": "browser_action",
                    "parameters": {"action": "navigate", "value": url + "/redirect/5"},
                },
                {"type": "http_request", "parameters": {"url": url + "/medium/1"}},
            ],
        }
    )
    store = MemoryEvidenceStore()
    http = HttpxAdapter()
    executor = RecipeExecutor(
        http,
        store,
        SourceSettings(),
        browser_factory=lambda: PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=3),
    )
    try:
        result = executor.execute(recipe, ExecutionContext(run_id=uuid4()))
        assert isinstance(result, CollectionRun)
        assert result.diagnostics.transport_attempts == 2
        assert result.diagnostics.response_bytes == 700 + len(b"<html><body>finished</body></html>")
        for _ in range(3):
            captured = store.get(result.evidence_reference)
            browser = CapturedBrowserSteps(captured["browser_steps"])
            replay_http = CapturedResponseHttpAdapter(captured["http_exchanges"])
            replay = executor.execute(
                recipe,
                ExecutionContext(run_id=uuid4()),
                http_adapter=replay_http,
                browser_replay=browser,
            )
            browser.assert_exhausted()
            replay_http.assert_exhausted()
            assert isinstance(replay, CollectionRun)
            assert replay.diagnostics.transport_attempts == result.diagnostics.transport_attempts
            assert replay.diagnostics.response_bytes == result.diagnostics.response_bytes
            result = replay
        assert len(requests) == 2
        failed = executor.execute(
            recipe,
            ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_total_response_bytes=710)),
        )
        assert isinstance(failed, SourceFailure)
        assert failed.error_code == "response_budget_exceeded"
        assert failed.diagnostics.response_bytes > 710
    finally:
        http.close()


def test_concurrent_mixed_runs_have_independent_budgets(
    budget_site: tuple[str, list[str]],
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore
    from tender_system.collection.recipes import RecipeExecutor
    from tender_system.core.configuration import SourceSettings
    from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecipe

    url, requests = budget_site
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "parallel",
            "version": "1",
            "steps": [
                {"type": "http_request", "parameters": {"url": url + "/medium/1"}},
                {
                    "type": "browser_action",
                    "parameters": {"action": "navigate", "value": url + "/redirect/5"},
                },
            ],
        }
    )

    def run() -> CollectionRun:
        http = HttpxAdapter()
        try:
            result = RecipeExecutor(
                http,
                MemoryEvidenceStore(),
                SourceSettings(),
                browser_factory=lambda: PlaywrightBrowserAdapter(
                    MemorySessionStore(), timeout_seconds=3
                ),
            ).execute(
                recipe, ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_requests=2))
            )
            assert isinstance(result, CollectionRun)
            return result
        finally:
            http.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert len(requests) == 4
    assert all(result.diagnostics.transport_attempts == 2 for result in results)
    assert all(result.diagnostics.browser_requests == 1 for result in results)


def test_browser_assets_consume_downloads_and_are_distinguished_from_documents(
    budget_site: tuple[str, list[str]],
) -> None:
    from uuid import uuid4

    from tender_system.collection.adapters import MemoryEvidenceStore
    from tender_system.collection.recipes import RecipeExecutor
    from tender_system.core.configuration import SourceSettings
    from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecipe
    from tender_system.testing.fakes import ScriptedHttpAdapter

    url, requests = budget_site
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "assets",
            "version": "1",
            "steps": [
                {
                    "type": "browser_action",
                    "parameters": {"action": "navigate", "value": url + "/asset-page"},
                },
            ],
        }
    )
    result = RecipeExecutor(
        ScriptedHttpAdapter(()),
        MemoryEvidenceStore(),
        SourceSettings(),
        browser_factory=lambda: PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=3),
    ).execute(recipe, ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_requests=20)))
    assert isinstance(result, CollectionRun)
    assert len(requests) == result.diagnostics.transport_attempts == 13
    assert result.diagnostics.browser_requests == 13
    assert result.diagnostics.browser_asset_requests == 12
    assert result.diagnostics.response_bytes >= 12 * 700


def test_browser_download_redirect_bodies_consume_total_budget(
    budget_site: tuple[str, list[str]],
) -> None:
    url, requests = budget_site
    browser = PlaywrightBrowserAdapter(
        MemorySessionStore(),
        timeout_seconds=3,
        limits=ExecutionLimits(max_response_bytes=800, max_total_response_bytes=1000),
    )
    try:
        with pytest.raises(ResponseBudgetExceeded):
            browser.download(url + "/download-redirect/0")
        assert requests == ["/download-redirect/0", "/medium/1"]
    finally:
        browser.close()
