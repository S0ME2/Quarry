from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from test_icppa import DEFINITION, FIXTURES, SOURCE, IcppaFixture, policy

from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecipe
from tender_system.core.ports import HttpRequest
from tender_system.testing.local_server import local_http_server, server_url


@pytest.mark.browser
def test_central_api_and_independent_rendered_witness_on_localhost() -> None:
    fixture = IcppaFixture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.respond(
                fixture.request(
                    HttpRequest("https://xarid.icppa.uz" + self.path, "POST", body=body)
                ).body,
                "application/json",
            )

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/api/common/GetTrade/"):
                self.respond(
                    fixture.request(HttpRequest("https://apietender.uzex.uz" + self.path)).body,
                    "application/json",
                )
            elif path.startswith("/api/v1/lot/public/"):
                self.respond(
                    fixture.request(HttpRequest("https://xarid.icppa.uz" + self.path)).body,
                    "application/json",
                )
            else:
                assert self.path == "/witness-26111006517270.html"
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
            (DEFINITION / "recipe.json")
            .read_text()
            .replace("https://xarid.icppa.uz/api/", base + "/api/")
            .replace("https://apietender.uzex.uz/api/", base + "/api/")
        )
        http = HttpxAdapter()
        browser = PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=5)
        try:
            run = RecipeExecutor(http, MemoryEvidenceStore(), policy()).execute(
                recipe, ExecutionContext(run_id=uuid4())
            )
            assert isinstance(run, CollectionRun) and len(run.raw_observations) == 11
            sample = next(
                r for r in run.raw_observations if r["external_tender_id"] == 26111006517270
            )
            assert sample["source_url"] == "https://xarid.icppa.uz/lot-info/26111006517270"
            browser.navigate(base + "/witness-26111006517270.html")
            browser.wait_for_selector(".start-amount")
            soup = BeautifulSoup(browser.content(), "html.parser")
            text = " ".join(soup.get_text(" ", strip=True).split())
            assert str(sample["title"]) in text and str(sample["buyer"]) in text
            assert "76 296 000 000,00" in text and sample["value"] == "76296000000.00"
            assert SOURCE.has_stable_external_id
            assert all(exchange.request.url.startswith(base) for exchange in browser.network())
        finally:
            browser.close()
            http.close()
