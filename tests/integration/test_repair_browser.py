import json
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import Engine

from tender_system.collection.adapters import HttpxAdapter, MemoryEvidenceStore, MemorySessionStore
from tender_system.collection.browser import PlaywrightBrowserAdapter
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.core.domain import (
    FixResult,
    RecipeRevision,
    SourceFailure,
    SourceRecipe,
    SourceRecord,
)
from tender_system.core.ports import BrowserAdapter
from tender_system.persistence.recipe_storage import SqlValidationEnvironment
from tender_system.persistence.storage import (
    SqlSourceRepository,
    session_factory,
)
from tender_system.repair.repair_adapters import RecipeRepairExecutors
from tender_system.repair.repair_validation import BrowserFieldVerifier, IndependentRepairValidator
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import FakeClock
from tender_system.testing.local_server import local_http_server, server_url

pytestmark = [pytest.mark.browser, pytest.mark.database]


@pytest.fixture
def repair_site() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            origin = f"http://127.0.0.1:{self.server.server_port}"  # type: ignore[attr-defined]
            status, kind = 200, "text/html"
            if parts.path == "/":
                body = (
                    b'<button id="load" onclick="fetch(\'/api/v2\').then(r=>r.json())'
                    b".then(d=>{document.querySelector('#ready').textContent='Loaded'})\">Load"
                    b'</button><div id="ready"></div><a id="list" href="/list">Tenders</a>'
                )
            elif parts.path == "/login":
                body = (
                    b'<form method="post" action="/login"><input id="user">'
                    b'<input id="password" type="password"><button id="login">Login</button></form>'
                )
            elif parts.path == "/private":
                if "fixture-session=yes" not in self.headers.get("Cookie", ""):
                    body = b'<p>SESSION_EXPIRED</p><a href="/login">Login</a>'
                else:
                    body = b'<h1 id="private">Authorized inventory</h1>'
            elif parts.path == "/list":
                body = "".join(
                    f'<article class="entry"><span class="uid">{i}</span>'
                    f'<h2 class="title">Router {i}</h2><b class="buyer">Agency</b>'
                    f'<span class="date">2026-12-01</span>'
                    f'<a href="{origin}/detail/{i}">Detail</a></article>'
                    for i in range(2)
                ).encode()
            elif parts.path.startswith("/detail/"):
                i = parts.path.rsplit("/", 1)[1]
                body = (
                    f'<span class="uid">{i}</span><h1 class="title">Router {i}</h1>'
                    '<b class="buyer">Agency</b><span class="date">2026-12-01</span>'
                ).encode()
            elif parts.path in {"/api/v2", "/pages", "/dates"}:
                page = int(parse_qs(parts.query).get("page", ["1"])[0])
                ids = [page - 1] if parts.path == "/pages" else [0, 1]
                items = [
                    {
                        "external_tender_id": str(i),
                        "title": f"Router {i}",
                        "buyer": "Agency",
                        "deadline": "2026-12-01",
                        "source_url": f"{origin}/detail/{i}",
                    }
                    for i in ids
                ]
                if parts.path == "/dates":
                    for item in items:
                        del item["deadline"]
                        item["new_date"] = "01.12.2026"
                body, kind = (
                    json.dumps({"items": items, "done": page >= 2}).encode(),
                    "application/json",
                )
            elif parts.path == "/download":
                body, kind = b"local tender document", "application/octet-stream"
            else:
                status, body = 404, b"Retired API or missing page"
            self.send_response(status)
            self.send_header("Content-Type", kind)
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


def definition(site: str, kind: str, fixed: bool) -> SourceRecipe:
    steps: list[dict[str, JsonValue]]
    if kind == "selector":
        steps = [
            {"type": "http_request", "parameters": {"url": site + "/list"}},
            {
                "type": "extract",
                "parameters": {
                    "format": "html",
                    "input": {"$ref": "response.body"},
                    "selector": "article.entry" if fixed else "article.old",
                    "fields": {
                        "external_tender_id": {"selector": ".uid", "required": True},
                        "title": {"selector": ".title", "required": True},
                        "buyer": {"selector": ".buyer"},
                        "deadline": {"selector": ".date"},
                        "source_url": {"selector": "a", "attribute": "href"},
                    },
                    "emit": True,
                },
            },
        ]
    elif kind == "pagination":
        steps = [
            {"type": "transform", "parameters": {"name": "identity", "input": 1, "output": "page"}},
            {
                "type": "loop",
                "parameters": {"until": {"$ref": "done"}, "max_iterations": 3 if fixed else 1},
                "steps": [
                    {
                        "type": "http_request",
                        "parameters": {"url": site + "/pages", "query": {"page": {"$ref": "page"}}},
                    },
                    {
                        "type": "extract",
                        "parameters": {
                            "format": "json",
                            "input": {"$ref": "response.body"},
                            "output": "document",
                        },
                    },
                    {
                        "type": "transform",
                        "parameters": {
                            "name": "get",
                            "input": {"$ref": "document"},
                            "arguments": {"path": "items"},
                            "emit": True,
                        },
                    },
                    {
                        "type": "transform",
                        "parameters": {
                            "name": "get",
                            "input": {"$ref": "document"},
                            "arguments": {"path": "done"},
                            "output": "done",
                        },
                    },
                    {
                        "type": "transform",
                        "parameters": {
                            "name": "add",
                            "input": {"$ref": "page"},
                            "arguments": {"other": 1},
                            "output": "page",
                        },
                    },
                ],
            },
        ]
    else:
        url = "/dates" if kind == "date" else "/api/v2" if fixed else "/api/v1"
        steps = [
            {"type": "http_request", "parameters": {"url": site + url}},
            {
                "type": "extract",
                "parameters": {
                    "format": "json",
                    "input": {"$ref": "response.body"},
                    "selector": "items",
                    "output": "items",
                    "emit": kind != "date",
                },
            },
        ]
        if kind == "date":
            steps.append(
                {
                    "type": "loop",
                    "parameters": {"items": {"$ref": "items"}},
                    "steps": [
                        {
                            "type": "transform",
                            "parameters": {
                                "name": "parse_date",
                                "input": {"$ref": "item.new_date" if fixed else "item.deadline"},
                                "arguments": {
                                    "settings": {
                                        "normalization": {
                                            "date_formats": {"deadline": ["%d.%m.%Y"]}
                                        }
                                    }
                                },
                                "output": "date",
                            },
                        },
                        {
                            "type": "transform",
                            "parameters": {
                                "name": "identity",
                                "input": {
                                    "external_tender_id": {"$ref": "item.external_tender_id"},
                                    "title": {"$ref": "item.title"},
                                    "buyer": {"$ref": "item.buyer"},
                                    "source_url": {"$ref": "item.source_url"},
                                    "deadline": {"$ref": "date"},
                                },
                                "emit": True,
                            },
                        },
                    ],
                }
            )
    return SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "2" if fixed else "1",
            "expected_count": 2,
            "completion_signal": {"kind": "total_reconciled", "value": 2},
            "steps": steps,
        }
    )


def test_browser_independent_witness_rejects_plausible_wrong_field(
    migrated_engine: Engine, repair_site: str
) -> None:
    # Direct source samples use operator-owned selectors, never the proposed extraction mapping.
    from tender_system.core.domain import CollectionRun
    from tender_system.repair.repair_validation import dry_run

    sessions = session_factory(migrated_engine)
    source = SourceRecord(id="s", name="Local")
    with sessions.begin() as session:
        SqlSourceRepository(session).add(source)
    clock, evidence = FakeClock(datetime(2026, 1, 1, tzinfo=UTC)), MemoryEvidenceStore()

    def browser_factory() -> BrowserAdapter:
        return PlaywrightBrowserAdapter(MemorySessionStore(), timeout_seconds=5)

    settings, normalizer = fixture_source_settings(), ConfigurableTenderNormalizer()
    proposed = definition(repair_site, "selector", True).model_dump(mode="json")
    # A buyer is syntactically valid title text, but source detail witness disproves it.
    proposed["steps"][1]["parameters"]["fields"]["title"]["selector"] = ".buyer"
    recipe = SourceRecipe.model_validate(proposed)
    environment = SqlValidationEnvironment(
        sessions, RecipeRepairExecutors(HttpxAdapter, browser_factory, evidence, clock)
    )
    failure = SourceFailure(
        run_id=uuid4(),
        source_id="s",
        recipe=definition(repair_site, "selector", False),
        error_code="broken",
        error_detail="Changed",
        failed_evidence_reference="failure",
    )
    run = dry_run(
        environment, normalizer, RecipeRevision(recipe=recipe), failure, source, settings, uuid4()
    )
    assert isinstance(run, CollectionRun)
    verifier = BrowserFieldVerifier(
        browser_factory, {"external_tender_id": ".uid", "title": ".title"}, normalizer, evidence
    )
    validation = IndependentRepairValidator(
        environment, normalizer, source, settings, verifier, evidence, clock
    ).validate(
        failure,
        FixResult(status="fixed", updated_recipe=recipe, explanation="Claimed fixed", confidence=1),
        run,
    )
    assert not validation.passed and any(r.code == "sample_mismatch" for r in validation.checks)
