"""URL-only onboarding through real HTTP, executor, browser witnesses and PostgreSQL."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from tender_system.api.api import create_app
from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.core.configuration import load_configuration
from tender_system.core.ports import ModelRequest, ModelResponse
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    OperatorConfigRow,
    SourceRow,
    TenderRow,
    session_factory,
)
from tender_system.testing.fakes import FunctionModelClient
from tender_system.testing.local_server import local_http_server, server_url

pytestmark = [pytest.mark.database, pytest.mark.browser]


@contextmanager
def generated_site(mode: str = "api", fault: str = "none") -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append(self.path)
            origin = server_url(self.server)  # type: ignore[arg-type]
            path = urlsplit(self.path).path
            page = int(parse_qs(urlsplit(self.path).query).get("page", ["1"])[0])
            kind = "text/html"
            if path == "/api":
                keys = [page] if page <= 2 and fault != "empty" else []
                body = json.dumps(
                    {
                        "items": [
                            {
                                "external_tender_id": str(i),
                                "title": f"Office routers {i}",
                                "buyer": "Procurement office",
                                "value": "999" if fault == "wrong_value" else "1234.50",
                                "currency": "USD",
                                "deadline": "2026-12-01",
                                "source_url": f"{origin}/detail/{i}",
                            }
                            for i in keys
                        ],
                        "next": f"{origin}/api?page={page + 1}" if keys else None,
                    }
                )
                if fault == "omission":
                    payload = json.loads(body)
                    for row in payload["items"]:
                        row.pop("buyer")
                    body = json.dumps(payload)
                kind = "application/json"
            elif path.startswith("/detail/"):
                key = path.rsplit("/", 1)[-1]
                body = f"""<!doctype html><html><body><h1>Office routers {key}</h1>
                <span id="id">{key}</span><span id="buyer">Procurement office</span>
                <span id="value">1234.50</span><span id="currency">USD</span>
                <time id="deadline">2026-12-01</time></body></html>"""
            elif mode == "html":
                body = (
                    "<html><body>"
                    + "".join(
                        f'<article><a href="{origin}/detail/{i}">Office routers {i}</a>'
                        f'<span class="id">{i}</span><span class="buyer">Procurement office</span>'
                        '<span class="value">1234.50</span><span class="currency">USD</span>'
                        "<time>2026-12-01</time></article>"
                        for i in (1, 2)
                    )
                    + "</body></html>"
                )
            elif mode == "browser":
                body = (
                    '<html><body><main></main><script>fetch("/api").then(r=>r.json())'
                    '.then(x=>document.querySelector("main").textContent=JSON.stringify(x));'
                    "</script></body></html>"
                )
            else:
                body = '<html><body><a href="/api">Tender API</a></body></html>'
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with local_http_server(Handler) as server:
        yield server_url(server), requests


def generated_recipe(url: str, mode: str) -> dict[str, Any]:
    if mode == "api":
        steps: list[dict[str, Any]] = [
            {
                "type": "transform",
                "parameters": {"name": "identity", "input": url + "/api", "output": "next"},
            },
            {
                "type": "loop",
                "parameters": {"until": {"$ref": "done"}},
                "steps": [
                    {
                        "type": "http_request",
                        "parameters": {"url": {"$ref": "next"}, "inventory_page": True},
                    },
                    {
                        "type": "extract",
                        "parameters": {
                            "input": {"$ref": "response.body"},
                            "format": "json",
                            "output": "payload",
                        },
                    },
                    {
                        "type": "extract",
                        "parameters": {
                            "input": {"$ref": "payload"},
                            "format": "json",
                            "selector": "items",
                            "emit": True,
                        },
                    },
                    {
                        "type": "transform",
                        "parameters": {
                            "name": "identity",
                            "input": {"$ref": "payload.next"},
                            "output": "next",
                        },
                    },
                    {
                        "type": "transform",
                        "parameters": {
                            "name": "equals",
                            "input": {"$ref": "next"},
                            "arguments": {"other": None},
                            "output": "done",
                        },
                    },
                ],
            },
        ]
    else:
        acquisition = [{"type": "http_request", "parameters": {"url": url, "inventory_page": True}}]
        content: Any = {"$ref": "response.body"}
        if mode == "browser":
            acquisition = [
                {
                    "type": "browser_action",
                    "parameters": {"action": "navigate", "value": url, "inventory_page": True},
                },
                {
                    "type": "browser_action",
                    "parameters": {"action": "wait_for_selector", "selector": "main"},
                },
                {"type": "browser_action", "parameters": {"action": "content", "output": "dom"}},
            ]
            # Browser-only fixture uses rendered JSON text for first page.
            content = {"$ref": "dom"}
            steps = acquisition + [
                {
                    "type": "extract",
                    "parameters": {
                        "input": content,
                        "format": "html",
                        "selector": "main",
                        "fields": {"text": {"selector": "."}},
                        "output": "main",
                    },
                },
                {
                    "type": "extract",
                    "parameters": {
                        "input": {"$ref": "main.0.text"},
                        "format": "json",
                        "selector": "items",
                        "emit": True,
                    },
                },
            ]
        else:
            steps = acquisition + [
                {
                    "type": "extract",
                    "parameters": {
                        "input": content,
                        "format": "html",
                        "selector": "article",
                        "emit": True,
                        "fields": {
                            "title": {"selector": "a"},
                            "external_tender_id": {"selector": ".id"},
                            "source_url": {"selector": "a", "attribute": "href"},
                            "buyer": {"selector": ".buyer"},
                            "value": {"selector": ".value"},
                            "currency": {"selector": ".currency"},
                            "deadline": {"selector": "time"},
                        },
                    },
                }
            ]
    return {
        "source_id": "model-owned",
        "version": "ignored",
        "coverage": "incremental",
        "steps": steps,
    }


@pytest.mark.parametrize(
    "mode,fault",
    [
        ("api", "none"),
        ("html", "none"),
        ("browser", "none"),
        ("api", "wrong_value"),
        ("api", "omission"),
        ("api", "empty"),
        ("api", "unsupported"),
        ("api", "broken"),
        ("api", "no_witness"),
        ("api", "repaired_schema"),
        ("api", "repaired_execution"),
        ("api", "repaired_normalization"),
    ],
)
def test_url_only_generation_activation_and_monitoring(
    migrated_engine: Any, mode: str, fault: str
) -> None:
    with generated_site(mode, fault) as (url, requests):
        sessions = session_factory(migrated_engine)
        config = load_configuration(validate_models=False)
        config.runtime.manual_only = False
        config.runtime.operator_worker_enabled = False
        calls: list[ModelRequest] = []

        def reply(request: ModelRequest) -> ModelResponse:
            calls.append(request)
            if request.task == "recipe_generation":
                if fault == "unsupported":
                    return ModelResponse(
                        {"actions": [], "recipe": None, "reason": "Login required"}, "fixture"
                    )
                observations = request.payload["observations"]
                if len(observations) == 1 and mode == "api":  # type: ignore[arg-type]
                    return ModelResponse({"kind": "get", "url": url + "/api"}, "fixture")
                recipe = generated_recipe(url, mode)
                if fault == "repaired_schema" and "engine_feedback" not in request.payload:
                    recipe["unsupported_field"] = True
                if fault == "repaired_normalization":
                    if "engine_feedback" not in request.payload:
                        recipe["steps"][1]["steps"][2]["parameters"]["fields"] = {
                            "title": {"selector": "title"},
                            "source_url": {"selector": "source_url"},
                        }
                    else:
                        feedback = request.payload["engine_feedback"]
                        assert isinstance(feedback, dict)
                        assert feedback["stage"] == "normalization"
                        assert feedback["normalization_errors"]
                        assert "verification" not in feedback and "samples" not in feedback
                if fault == "broken" or (
                    fault == "repaired_execution" and "engine_feedback" not in request.payload
                ):
                    recipe["steps"][1]["steps"][2]["parameters"]["selector"] = "nonexistent"
                return ModelResponse({"recipe": recipe}, "fixture")
            assert request.task == "recipe_witness_inventory"
            assert "recipe" not in request.payload and "candidates" not in request.payload
            fields = {
                name: {"selector": selector, "required": True}
                for name, selector in {
                    "external_tender_id": "#id",
                    "title": "h1",
                    "buyer": "#buyer",
                    "value": "#value",
                    "currency": "#currency",
                    "deadline": "#deadline",
                }.items()
            }
            if fault == "no_witness":
                fields.pop("title")
            return ModelResponse(
                {
                    "fields": cast(JsonValue, fields),
                    "unavailable_fields": {
                        name: "source_absent"
                        for name in (
                            "description",
                            "requirements",
                            "conditions",
                            "location",
                            "published_at",
                            "status",
                            "document_urls",
                            "product_items",
                        )
                    },
                },
                "fixture",
            )

        runtime = OperatorRuntime(
            sessions,
            config,
            evidence=MemoryEvidenceStore(),
            models=lambda _: FunctionModelClient(reply),
            public_url_check=lambda _: None,
        )
        # Real guarded browser; explicit test-only checker permits the owned local server.
        try:
            with TestClient(create_app(sessions, config, runtime=runtime)) as client:
                response = client.post("/api/v1/web/sources", json={"url": url})
                assert response.status_code == 201, response.text
                data = response.json()
                key, operation_id = data["source_id"], UUID(data["operation_id"])
                with sessions() as session:
                    operation = session.get(OperationRow, operation_id)
                    assert operation is not None
                    if fault not in {
                        "none",
                        "repaired_schema",
                        "repaired_execution",
                        "repaired_normalization",
                    }:
                        assert operation.status == "failed", operation.result
                        assert session.get(SourceRow, key) is None
                        assert session.get(ActiveRecipeRow, key) is None
                        assert runtime.schedule_due() == 0
                        if fault in {"wrong_value", "omission", "no_witness"}:
                            assert (
                                operation.result["error_code"] == "independent_verification_failed"
                            )
                        return
                    assert operation.status == "activated", {
                        "error_code": operation.error_code,
                        **operation.result,
                    }
                    assert operation.result["validation_passed"] is True
                    assert operation.result["replay_passed"] is True
                    verification = operation.result["verification"]
                    assert isinstance(verification, dict)
                    assert int(str(verification["checked"])) >= 1
                    assert operation.result["inventory_authority"] is False
                    if fault.startswith("repaired_"):
                        assert operation.result["recipe_revisions"] == 1
                    assert session.get(SourceRow, key)
                    assert session.get(ActiveRecipeRow, key)
                    control = session.get(OperatorConfigRow, "source/" + key)
                    assert control and control.value["manual_only"] is False
                assert any(call.task == "recipe_witness_inventory" for call in calls)
                assert requests.count("/api") >= 2 if mode == "api" else True
                assert runtime.schedule_due() == 1
                assert runtime.tick()
                with sessions() as session:
                    from sqlalchemy import select

                    collected = session.scalar(
                        select(OperationRow).where(OperationRow.kind == "collection")
                    )
                    assert collected and collected.status == "completed", (
                        collected.result if collected else None
                    )
                    assert list(session.scalars(select(TenderRow)))
        finally:
            runtime.close()


@pytest.mark.skipif(
    os.environ.get("QUARRY_LIVE_GOLDEN") != "1", reason="Explicit live provider opt-in required"
)
def test_live_provider_url_only_on_owned_site(migrated_engine: Any) -> None:
    """Optional real configured provider; no hand-authored recipe or scripted model reply."""
    with generated_site("api") as (url, _):
        sessions = session_factory(migrated_engine)
        config = load_configuration(validate_models=False)
        config.runtime.operator_worker_enabled = False
        config.runtime.operation_timeout_seconds = 360
        base_model = config.for_campaign("onboarding").model
        generation_model = base_model.model_copy(
            update={
                "max_output_tokens": 6000,
                "max_attempts": 1,
                "timeout_seconds": 120,
                "request_interval_seconds": max(
                    base_model.request_interval_seconds,
                    61 if base_model.provider == "groq" else 0,
                ),
            }
        )
        investigation = config.global_settings.source.investigation.model_copy(
            update={"timeout_seconds": 210}
        )
        config = config.model_copy(
            update={
                "global_settings": config.global_settings.model_copy(
                    update={
                        "source": config.global_settings.source.model_copy(
                            update={
                                "fixer_model": generation_model,
                                "investigation": investigation,
                            }
                        )
                    }
                )
            }
        )
        evidence = MemoryEvidenceStore()
        runtime = OperatorRuntime(
            sessions, config, evidence=evidence, public_url_check=lambda _: None
        )
        try:
            with TestClient(create_app(sessions, config, runtime=runtime)) as client:
                response = client.post("/api/v1/web/sources", json={"url": url})
                assert response.status_code == 201
                key = UUID(response.json()["operation_id"])
                with sessions() as session:
                    operation = session.get(OperationRow, key)
                    assert operation is not None
                    from tender_system.core.environment import PROJECT_ROOT

                    destination = (
                        PROJECT_ROOT
                        / "artifacts/tests/recipe-automation"
                        / generation_model.provider
                        / uuid4().hex
                    )
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / "controlled-live-result.json").write_text(
                        json.dumps(
                            {**operation.result, "operation_error_code": operation.error_code},
                            ensure_ascii=False,
                        )
                    )
                    (destination / "controlled-live-evidence.json").write_text(
                        json.dumps(evidence._values, ensure_ascii=False)
                    )
                    assert operation.status == "activated", {
                        "error_code": operation.error_code,
                        **operation.result,
                    }
                    assert operation.result["validation_passed"] is True
                    assert operation.result["replay_passed"] is True
        finally:
            runtime.close()
