from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from time import monotonic
from uuid import uuid4

import pytest
from playwright.sync_api import TimeoutError as BrowserTimeout
from sqlalchemy import Engine

from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecipe
from tender_system.core.execution import execution_budget
from tender_system.core.ports import BrowserAdapter, HttpRequest
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.local_server import local_http_server, server_url

pytestmark = pytest.mark.browser


def test_reused_browser_action_obeys_remaining_operation_budget() -> None:
    browser = PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=5)
    try:
        started = monotonic()
        with execution_budget(0.05), pytest.raises(BrowserTimeout):
            browser.wait_for_selector("#never-created")
        assert monotonic() - started < 1
    finally:
        browser.close()


@pytest.fixture
def fixture_site() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            status, content_type = 200, "text/html"
            if path == "/login":
                body = (
                    b'<form method="post" action="/login"><input id="username" name="username">'
                    b'<input id="password" name="password" type="password">'
                    b'<button id="login">Login</button></form>'
                )
            elif path == "/expired":
                body = b'<div id="terminal">SESSION_EXPIRED</div><p>NO_ACTIVE_TENDERS</p>'
            elif path == "/private":
                if "fixture-session=yes" not in self.headers.get("Cookie", ""):
                    status, body = 401, b"Unauthorized"
                else:
                    body = b'<h1>Private tenders</h1><article data-id="9">Equipment</article>'
            elif path == "/api":
                body = b'{"items": [{"external_tender_id": "1", "title": "API equipment"}]}'
                content_type = "application/json"
            elif path == "/download":
                body, content_type = b"fixture bytes", "application/octet-stream"
            elif path == "/":
                body = b"""<input id="text"><select id="choice"><option value="a">A</option>
                <option value="b">B</option></select><button id="load" onclick="fetch('/api')
                .then(r=>r.json()).then(d=>{document.querySelector('#result').textContent=d.items[0].title;
                document.querySelector('#result').dataset.ready='true'})">Load</button>
                <div id="result"></div><div style="height:2000px"></div>"""
            else:
                status, body = 404, b"Unknown fixture"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(303)
            self.send_header("Set-Cookie", "fixture-session=yes; HttpOnly; Path=/")
            self.send_header("Location", "/private")
            self.send_header("Content-Length", "0")
            self.end_headers()

    with local_http_server(Handler) as server:
        yield server_url(server)


def test_playwright_browser_contract_local_fixture(fixture_site: str) -> None:
    adapter: BrowserAdapter = PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=5)
    try:
        adapter.navigate(fixture_site)
        adapter.type_text("#text", "fixture")
        adapter.select("#choice", "b")
        adapter.scroll(500)
        adapter.click("#load")
        adapter.wait_for_selector('#result[data-ready="true"]')
        adapter.wait_for_response(fixture_site + "/api")
        assert "API equipment" in adapter.content()
        api = next(x for x in adapter.network() if x.request.url.endswith("/api"))
        assert api.response is not None and b"API equipment" in api.response.body
        assert not api.request.headers and api.request.body is None
        assert adapter.download(fixture_site + "/download") == b"fixture bytes"
        assert adapter.screenshot().startswith(b"\x89PNG")
        assert adapter.trace().startswith(b"PK")
    finally:
        adapter.close()


def test_login_and_session_reuse(fixture_site: str) -> None:
    sessions = MemorySessionStore()
    first = PlaywrightBrowserAdapter(sessions, timeout_seconds=5)
    try:
        first.navigate(fixture_site + "/login")
        first.type_text("#username", "fixture-user")
        first.type_text("#password", "fixture-value")
        first.click("#login")
        first.wait_for_selector("article")
        reference = first.save_session()
        assert "Private tenders" in first.content()
        assert all(not x.request.headers and x.request.body is None for x in first.network())
    finally:
        first.close()
    second = PlaywrightBrowserAdapter(sessions, timeout_seconds=5)
    try:
        second.restore_session(reference)
        second.navigate(fixture_site + "/private")
        assert "Equipment" in second.content()
    finally:
        second.close()


def network_recipe(fixture_site: str) -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "1",
            "completion_signal": {"kind": "end_marker", "value": {"$ref": "loaded"}},
            "steps": [
                {
                    "type": "browser_action",
                    "parameters": {
                        "action": "navigate",
                        "value": fixture_site,
                        "inventory_page": True,
                    },
                },
                {"type": "browser_action", "parameters": {"action": "click", "selector": "#load"}},
                {
                    "type": "browser_action",
                    "parameters": {
                        "action": "wait_for_selector",
                        "selector": '#result[data-ready="true"]',
                        "output": "loaded",
                    },
                },
                {
                    "type": "browser_action",
                    "parameters": {"action": "network", "output": "network"},
                },
                {
                    "type": "loop",
                    "parameters": {"items": {"$ref": "network"}},
                    "steps": [
                        {
                            "type": "transform",
                            "parameters": {
                                "name": "equals",
                                "input": {"$ref": "item.url"},
                                "arguments": {"other": fixture_site + "/api"},
                                "output": "is_api",
                            },
                        },
                        {
                            "type": "condition",
                            "parameters": {"predicate": {"$ref": "is_api"}},
                            "steps": [
                                {
                                    "type": "extract",
                                    "parameters": {
                                        "input": {"$ref": "item.body"},
                                        "format": "json",
                                        "selector": "items",
                                        "emit": True,
                                    },
                                },
                            ],
                        },
                    ],
                },
            ],
        }
    )


def test_browser_recipe_network_api_capture(fixture_site: str) -> None:
    recipe = network_recipe(fixture_site)
    http = HttpxAdapter()
    try:
        executor = RecipeExecutor(
            http,
            MemoryEvidenceStore(),
            SourceSettings(),
            browser_factory=lambda: PlaywrightBrowserAdapter(
                MemorySessionStore(), timeout_seconds=5
            ),
        )
        result = executor.execute(recipe, ExecutionContext(run_id=uuid4()))
        assert isinstance(result, CollectionRun)
        assert result.raw_observations == ({"external_tender_id": "1", "title": "API equipment"},)
    finally:
        http.close()


def test_http_adapter_against_local_server(fixture_site: str) -> None:
    adapter = HttpxAdapter()
    try:
        response = adapter.request(HttpRequest(fixture_site + "/api"))
        assert response.status_code == 200 and b"API equipment" in response.body
        assert adapter.request(HttpRequest(fixture_site + "/missing")).status_code == 404
    finally:
        adapter.close()


@pytest.mark.database
def test_browser_source_run_end_to_end(fixture_site: str, migrated_engine: Engine) -> None:
    from tender_system.collection.normalization import ConfigurableTenderNormalizer
    from tender_system.collection.source_runs import SourceRunService
    from tender_system.core.domain import SourceRecord
    from tender_system.persistence.storage import (
        SqlSourceRepository,
        SqlTenderRepository,
        TransactionalRunStore,
        session_factory,
    )

    source = SourceRecord(id="s", name="Local browser fixture")
    sessions = session_factory(migrated_engine)
    with sessions.begin() as session:
        SqlSourceRepository(session).add(source)
    http = HttpxAdapter()
    try:
        executor = RecipeExecutor(
            http,
            MemoryEvidenceStore(),
            fixture_source_settings(),
            browser_factory=lambda: PlaywrightBrowserAdapter(
                MemorySessionStore(), timeout_seconds=5
            ),
        )
        service = SourceRunService(
            executor, ConfigurableTenderNormalizer(), TransactionalRunStore(sessions)
        )
        run_id = uuid4()
        result = service.run(
            run_id, network_recipe(fixture_site), source, fixture_source_settings()
        )
        assert result.status == "accepted"
        assert (
            service.run(run_id, network_recipe(fixture_site), source, fixture_source_settings())
            == result
        )
        with sessions() as session:
            tender = SqlTenderRepository(session).for_source("s")[0]
            assert tender.title == "API equipment" and tender.external_tender_id == "1"
            assert len(SqlTenderRepository(session).history(tender.internal_id)) == 1
            assert tender.presence.missing_successful_runs == 0
    finally:
        http.close()


@pytest.mark.database
@pytest.mark.parametrize("wait_for_missing_marker", [False, True])
def test_expired_browser_session_never_establishes_empty_inventory(
    fixture_site: str, migrated_engine: Engine, wait_for_missing_marker: bool
) -> None:
    import json
    from datetime import UTC, datetime

    from tender_system.collection.normalization import ConfigurableTenderNormalizer
    from tender_system.collection.source_runs import SourceRunService
    from tender_system.core.domain import SourceRecord, TenderIdentity
    from tender_system.persistence.storage import (
        SqlSourceRepository,
        SqlTenderRepository,
        TransactionalRunStore,
        session_factory,
    )
    from tender_system.testing.fakes import FakeClock, ScriptedHttpAdapter

    sessions = session_factory(migrated_engine)
    source = SourceRecord(id="fixture", name="Browser fixture")
    with sessions.begin() as session:
        SqlSourceRepository(session).add(source)
    evidence = MemoryEvidenceStore()
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = TransactionalRunStore(sessions, clock=clock)
    seed_policy = fixture_source_settings()
    seed_recipe = SourceRecipe.model_validate(
        {
            "source_id": source.id,
            "version": "seed",
            "expected_count": 2,
            "completion_signal": {"kind": "total_reconciled", "value": 2},
            "steps": [
                {
                    "type": "transform",
                    "parameters": {
                        "name": "identity",
                        "emit": True,
                        "input": [
                            {"external_tender_id": "a", "title": "Equipment"},
                            {"external_tender_id": "b", "title": "Equipment"},
                        ],
                    },
                }
            ],
        }
    )
    seed = SourceRunService(
        RecipeExecutor(ScriptedHttpAdapter(()), evidence, seed_policy, clock=clock),
        ConfigurableTenderNormalizer(),
        store,
        clock=clock,
    ).run(uuid4(), seed_recipe, source, seed_policy)
    assert seed.status == "accepted"
    values = fixture_source_settings().model_dump()
    values["health"]["allow_empty"] = True
    values["calibration"].update(
        empty_policy="signature",
        empty_signatures=["NO_ACTIVE_TENDERS"],
        session_expiry_signatures=["SESSION_EXPIRED"],
    )
    policy = SourceSettings.model_validate(values)
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "fixture",
            "version": "expired-browser-1",
            "completion_signal": {"kind": "end_marker", "value": {"$ref": "terminal"}},
            "steps": [
                {
                    "type": "browser_action",
                    "parameters": {
                        "action": "navigate",
                        "value": fixture_site + "/expired",
                        "inventory_page": True,
                    },
                },
                {
                    "type": "browser_action",
                    "parameters": {
                        "action": "wait_for_selector",
                        "selector": "#missing" if wait_for_missing_marker else "#terminal",
                        "output": "terminal",
                    },
                },
                {"type": "browser_action", "parameters": {"action": "content", "output": "dom"}},
                {
                    "type": "extract",
                    "parameters": {
                        "format": "html",
                        "input": {"$ref": "dom"},
                        "selector": "article",
                        "require_match": False,
                        "emit": True,
                    },
                },
            ],
        }
    )
    http = HttpxAdapter()
    try:
        service = SourceRunService(
            RecipeExecutor(
                http,
                evidence,
                policy,
                clock=clock,
                browser_factory=lambda: PlaywrightBrowserAdapter(
                    MemorySessionStore(), timeout_seconds=5
                ),
            ),
            ConfigurableTenderNormalizer(),
            store,
            clock=clock,
        )
        result = service.run(uuid4(), recipe, SourceRecord(id="fixture", name="Fixture"), policy)
        assert result.status == ("failed" if wait_for_missing_marker else "rejected")
        assert result.health is not None
        assert result.health.dimensions["session"] == "fail"
        assert result.health.empty_classification == "suspicious_empty"
        assert not result.health.absence_evidence_allowed
        with sessions() as session:
            tender = SqlTenderRepository(session).find(
                TenderIdentity(source_id=source.id, external_tender_id="b")
            )
            assert tender is not None and tender.presence.missing_successful_runs == 0
        assert "SESSION_EXPIRED" in json.dumps(evidence.get(result.evidence_reference))
    finally:
        http.close()


def test_playwright_resolved_url_with_occupied_debug_port(fixture_site: str) -> None:
    from urllib.parse import urlsplit
    from urllib.request import urlopen

    class ConflictHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = b"<h1>Isolated E2E server</h1>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    occupied = urlsplit(fixture_site).port
    assert occupied is not None
    with local_http_server(ConflictHandler, port=occupied) as server:
        assert server.server_port != occupied
        browser = PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=5)
        try:
            browser.navigate(server_url(server))
            assert "Isolated E2E server" in browser.content()
        finally:
            browser.close()
    with urlopen(fixture_site + "/api", timeout=2) as response:
        assert b"API equipment" in response.read()


@pytest.mark.parametrize("wait_until,checked", [("load", 0), ("domcontentloaded", 1)])
def test_independent_witness_can_verify_content_before_unrelated_assets_load(
    wait_until: str, checked: int
) -> None:
    from threading import Event
    from typing import Literal, cast

    from tender_system.collection.verification import BrowserWitnesses
    from tender_system.core.domain import SourceRecord, TenderCandidate

    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/slow.svg":
                release.wait(5)
                body = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
                kind = "image/svg+xml"
            else:
                body = (
                    b'<h1 id="title"></h1><img src="/slow.svg"><script>'
                    b'setTimeout(()=>document.querySelector("#title").textContent='
                    b'"Verified equipment",50)</script>'
                )
                kind = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with local_http_server(Handler) as server:
        browser = PlaywrightBrowserAdapter(
            MemorySessionStore(),
            timeout_seconds=1,
            navigation_wait_until=cast(Literal["load", "domcontentloaded"], wait_until),
        )
        now = datetime.now(UTC)
        run = CollectionRun(
            id=uuid4(),
            source_id="fixture",
            recipe_version="1",
            started_at=now,
            finished_at=now,
            complete=False,
            evidence_reference="fixture",
            candidates=(
                TenderCandidate(
                    source_id="fixture",
                    external_tender_id="123",
                    title="Verified equipment",
                    source_url=server_url(server) + "/detail/123",
                ),
            ),
        )
        try:
            result = BrowserWitnesses(lambda: browser, MemoryEvidenceStore()).verify(
                run, SourceRecord(id="fixture", name="Fixture"), SourceSettings()
            )
            assert result.checked == checked
            if checked:
                assert result.mismatches == ()
            else:
                assert result.mismatches[0].code == "sample_timeout"
                assert "navigation" in result.mismatches[0].detail
        finally:
            release.set()
