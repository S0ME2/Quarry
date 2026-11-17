"""Owned HTTP source, isolated application composition and semantic mock for web tests."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from sqlalchemy import Engine

from tender_system.api.api import create_app
from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import Configuration
from tender_system.core.ports import ModelRequest, ModelResponse
from tender_system.operator.matching_budget import MatchingBudget
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import OperatorConfigRow, session_factory
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import FunctionModelClient
from tender_system.testing.local_server import local_http_server, server_url
from tender_system.testing.operator import fixture_recipe


@dataclass
class WebFixture:
    app: FastAPI
    runtime: OperatorRuntime
    source_url: str
    calls: list[ModelRequest] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    fail_model: bool = False
    break_source: bool = False
    model_behavior: str = "success"
    alternate_listing: bool = False
    changed_title: bool = False
    record_count: int = 2
    document_urls: list[str] = field(default_factory=list)
    requirements: str | None = None
    conditions: str | None = None

    @property
    def recipe(self) -> str:
        return fixture_recipe("web-source", self.source_url).model_dump_json()


@contextmanager
def web_fixture(engine: Engine, config: Configuration | None = None) -> Iterator[WebFixture]:
    calls: list[ModelRequest] = []
    requests: list[str] = []
    fixture: WebFixture

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append(self.path)
            if fixture.break_source:
                self.send_error(503)
                return
            if urlsplit(self.path).path == "/api":
                body = json.dumps(
                    {
                        "items": [
                            {
                                "external_tender_id": str(i),
                                "title": f"Network equipment {i}"
                                + (" changed" if fixture.changed_title else ""),
                                "description": "Network router for office connectivity",
                                "buyer": "Public procurement agency",
                                "deadline": "2026-12-01",
                                "value": "120000",
                                "currency": "USD",
                                "status": "open",
                                "source_url": fixture.source_url + f"/detail/{i}",
                                "product_items": [{"name": "Network router", "quantity": 2}],
                                "document_urls": fixture.document_urls,
                                "requirements": fixture.requirements,
                                "conditions": fixture.conditions,
                            }
                            for i in range(fixture.record_count)
                        ],
                        "total": fixture.record_count,
                    }
                ).encode()
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                if query:
                    payload = json.loads(body)
                    payload["items"] = [
                        item
                        for item in payload["items"]
                        if query.casefold() in item["title"].casefold()
                    ]
                    payload["total"] = len(payload["items"])
                    body = json.dumps(payload).encode()
                if fixture.alternate_listing:
                    payload = json.loads(body)
                    payload["lots"] = payload.pop("items")
                    body = json.dumps(payload).encode()
                kind = "application/json"
            else:
                key = self.path.rsplit("/", 1)[-1]
                body = (
                    f"<h1>Network equipment {key}</h1><p>Tender ID {key}</p>"
                    "<p>Network router for office connectivity</p>"
                ).encode()
                kind = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def model(request: ModelRequest) -> ModelResponse:
        calls.append(request)
        if fixture.fail_model or fixture.model_behavior == "unavailable":
            from tender_system.core.ports import ModelFailure

            raise ModelFailure("provider_unavailable")
        if fixture.model_behavior == "invalid_response":
            return ModelResponse({"unexpected": True}, "web-mock-v1")
        if fixture.model_behavior == "unsure":
            return ModelResponse(
                {
                    "decision": "unsure",
                    "confidence": 0.4,
                    "explanation": "Insufficient specification",
                },
                "web-mock-v1",
            )
        return ModelResponse(
            {
                "decision": "match",
                "confidence": 0.95,
                "explanation": "Network router is explicitly present",
                "evidence": [
                    {
                        "field": "product_items.0.name",
                        "quote": "Fabricated evidence"
                        if fixture.model_behavior == "invalid_evidence"
                        else "Network router",
                    }
                ],
            },
            "web-mock-v1",
            input_tokens=100,
            output_tokens=700 if fixture.model_behavior == "overrun" else 40,
        )

    config = config or Configuration.model_validate(
        {
            "global": {"source": fixture_source_settings().model_dump(mode="json")},
            "runtime": {
                "matching_enabled": True,
                "ai_semantic_enabled": True,
                "manual_only": True,
                "operator_worker_enabled": False,
            },
        }
    )
    with local_http_server(Handler) as server:
        runtime = OperatorRuntime(
            session_factory(engine),
            config,
            evidence=MemoryEvidenceStore(),
            models=lambda _: FunctionModelClient(model),
        )
        with runtime.sessions.begin() as session:
            session.add(
                OperatorConfigRow(
                    key="matching_budget",
                    value=MatchingBudget(
                        global_limit_usd=10,
                        provider="groq",
                        model="qwen/qwen3.8-27b",
                        input_usd_per_million=1,
                        output_usd_per_million=1,
                        max_requests_per_run=100,
                    ).model_dump(mode="json"),
                    updated_at=SystemClock().now(),
                )
            )
        fixture = WebFixture(
            create_app(session_factory(engine), config, runtime=runtime),
            runtime,
            server_url(server),
            calls,
            requests,
        )
        try:
            yield fixture
        finally:
            runtime.close()
