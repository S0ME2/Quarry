"""Deterministic source investigation fixtures. Never used by production composition."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler

from tender_system.core.domain import SourceRecipe
from tender_system.testing.local_server import local_http_server, server_url


def fixture_recipe(source_id: str, url: str) -> SourceRecipe:
    return SourceRecipe.model_validate(
        {
            "source_id": source_id,
            "version": "discovered-v1",
            "expected_count": 2,
            "completion_signal": {"kind": "total_reconciled", "value": 2},
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {"url": url + "/api", "inventory_page": True},
                },
                {
                    "type": "extract",
                    "parameters": {
                        "format": "json",
                        "input": {"$ref": "response.body"},
                        "selector": "items",
                        "emit": True,
                    },
                },
            ],
        }
    )


@contextmanager
def onboarding_site(request_log: list[str] | None = None) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if request_log is not None:
                request_log.append(self.path)
            origin = server_url(self.server)  # type: ignore[arg-type]
            kind = "text/html"
            if self.path == "/api":
                body = json.dumps(
                    {
                        "items": [
                            {
                                "external_tender_id": str(i),
                                "title": f"Network equipment {i}",
                                "buyer": "Public procurement agency",
                                "deadline": "2026-12-01",
                                "value": "120000",
                                "currency": "USD",
                                "source_url": f"{origin}/detail/{i}",
                            }
                            for i in range(2)
                        ],
                        "total": 2,
                    }
                ).encode()
                kind = "application/json"
            elif self.path.startswith("/detail/"):
                key = self.path.rsplit("/", 1)[-1]
                body = f"<h1>Network equipment {key}</h1><p>Tender ID {key}</p>".encode()
            else:
                body = (
                    b'<h1>Procurement inventory</h1><a href="/api">Public JSON API (2 tenders)</a>'
                )
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with local_http_server(Handler) as server:
        yield server_url(server)
