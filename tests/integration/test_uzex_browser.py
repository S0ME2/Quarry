from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from test_uzex import UzexFixture

from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.configuration import load_configuration
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecipe, SourceRecord
from tender_system.core.ports import HttpRequest
from tender_system.testing.local_server import local_http_server, server_url

ROOT = Path(__file__).resolve().parents[2]
DEFINITION = ROOT / "sources" / "legacy" / "uzex"
FIXTURES = ROOT / "tests" / "fixtures" / "uzex"


@pytest.mark.browser
def test_captured_uzex_protocol_and_browser_witnesses_on_owned_localhost() -> None:
    fixture = UzexFixture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            assert self.path == "/api/common/TradeList"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.respond(
                fixture.request(
                    HttpRequest("https://apietender.uzex.uz" + self.path, "POST", body=body)
                ).body,
                "application/json",
            )

        def do_GET(self) -> None:
            if self.path.startswith("/api/common/GetTrade/"):
                body = fixture.request(HttpRequest("https://apietender.uzex.uz" + self.path)).body
                self.respond(body, "application/json")
            else:
                assert self.path in {
                    "/witness-513636.html",
                    "/witness-512867.html",
                    "/witness-508078.html",
                }
                self.respond((FIXTURES / self.path[1:]).read_bytes(), "text/html; charset=utf-8")

        def respond(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with local_http_server(Handler) as server:
        base = server_url(server)
        recipe = SourceRecipe.model_validate_json(
            (DEFINITION / "recipe.json").read_text().replace("https://apietender.uzex.uz", base)
        )
        settings = load_configuration(DEFINITION / "config.toml").for_source("uzex-etender")
        source = SourceRecord.model_validate_json((DEFINITION / "source.json").read_text())
        http = HttpxAdapter()
        browser = PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=5)
        try:
            run = RecipeExecutor(http, MemoryEvidenceStore(), settings).execute(
                recipe, ExecutionContext(run_id=uuid4())
            )
            assert isinstance(run, CollectionRun) and len(run.raw_observations) == 21
            normalized = [
                ConfigurableTenderNormalizer().normalize(raw, source, settings)
                for raw in run.raw_observations
            ]
            assert all(n.candidate is not None and not n.issues for n in normalized)
            for identifier in [513636, 512867, 508078]:
                row = next(
                    raw for raw in run.raw_observations if raw["external_tender_id"] == identifier
                )
                browser.navigate(f"{base}/witness-{identifier}.html")
                browser.wait_for_selector(".lot-item__title")
                soup = BeautifulSoup(browser.content(), "html.parser")
                text = " ".join(soup.get_text(" ", strip=True).split())
                assert soup.select_one(f'a[href="/lot/{identifier}"]') is not None
                assert str(row["title"]) in text and str(row["buyer"]) in text
                assert isinstance(row["deadline"], str)
                assert datetime.fromisoformat(row["deadline"]).strftime("%d-%m-%Y %H:%M") in text
                value = row["value"]
                assert isinstance(value, (int, float))
                assert f"{value:,.2f}".rstrip("0").rstrip(".") + " " + str(row["currency"]) in text
                # Requests all target the local server; public origins appear only as fixture data.
                assert browser.network()[-1].request.url.startswith(base)
            assert len(fixture.requests) == 25  # 3 pages + 21 details + first-page witness
        finally:
            browser.close()
            http.close()
