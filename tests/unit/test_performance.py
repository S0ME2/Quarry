import json
from pathlib import Path
from uuid import uuid4

import pytest

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.performance import PerformanceTrace
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import CollectionRun, ExecutionContext, SourceRecipe
from tender_system.core.ports import HttpResponse
from tender_system.testing.fakes import ScriptedHttpAdapter


def test_trace_is_opt_in_and_does_not_change_recipe_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "v1",
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {
                        "url": "https://fixture.test/api/TradeList?access_token=secret",
                        "method": "GET",
                        "inventory_page": True,
                    },
                },
                {
                    "type": "extract",
                    "parameters": {
                        "input": {"$ref": "response.body"},
                        "format": "json",
                        "selector": "rows",
                        "output": "rows",
                        "emit": True,
                    },
                },
            ],
        }
    )
    response = HttpResponse("https://fixture.test/api/TradeList", 200, b'{"rows": [{"id": 1}]}')

    def run(trace: PerformanceTrace | None) -> CollectionRun:
        executor = RecipeExecutor(
            ScriptedHttpAdapter((response,)), MemoryEvidenceStore(), SourceSettings()
        )
        if trace is None:
            result = executor.execute(recipe, ExecutionContext(run_id=uuid4()))
        else:
            with trace.activate():
                result = executor.execute(recipe, ExecutionContext(run_id=uuid4()))
        assert isinstance(result, CollectionRun)
        return result

    untraced = run(None)
    trace = PerformanceTrace()
    traced = run(trace)
    assert traced.raw_observations == untraced.raw_observations == ({"id": 1},)
    assert [e["category"] for e in trace.events].count("http_request") == 1
    assert any(e["category"] == "json_parse" for e in trace.events)
    report = trace.report()
    assert report["http"]["request_count"] == 1
    assert report["http"]["requests"][0]["response_bytes"] == len(response.body)
    assert report["http"]["requests"][0]["name"] == "Inventory page 1"
    assert report["http"]["requests"][0]["endpoint"] == "/api/TradeList"
    trace.log_summary(report)
    assert "[HTTP] Inventory page 1:" in capsys.readouterr().out
    encoded = json.dumps(report)
    assert "access_token" not in encoded
    assert "secret" not in encoded
    assert response.body.decode() not in encoded


def test_report_is_machine_readable_and_trace_event_limit_is_bounded(tmp_path: Path) -> None:
    trace = PerformanceTrace(max_events=1)
    trace.record("sample", "one", 1_000_000)
    trace.record("sample", "two", 2_000_000)
    target = tmp_path / "performance.json"
    report = trace.write_report(target)
    assert json.loads(target.read_text()) == report
    assert report["dropped_events"] == 1
    assert report["timing_semantics"]["do_not_sum_nested_categories"] is True


def test_trace_measures_loop_overhead() -> None:
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "v1",
            "steps": [
                {
                    "type": "loop",
                    "parameters": {"items": [1, 2], "variable": "item"},
                    "steps": [
                        {
                            "type": "transform",
                            "parameters": {
                                "name": "identity",
                                "input": {"$ref": "item"},
                                "output": "copy",
                            },
                        }
                    ],
                }
            ],
        }
    )
    trace = PerformanceTrace()
    result = RecipeExecutor(ScriptedHttpAdapter(()), MemoryEvidenceStore(), SourceSettings())
    with trace.activate():
        execution = result.execute(recipe, ExecutionContext(run_id=uuid4()))
    assert isinstance(execution, CollectionRun)
    assert sum(event["category"] == "loop_overhead" for event in trace.events) == 2


def test_trace_sanitizes_and_names_detail_endpoint() -> None:
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "s",
            "version": "v1",
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {"url": "https://fixture.test/api/GetTrade/512867/0"},
                }
            ],
        }
    )
    trace = PerformanceTrace()
    executor = RecipeExecutor(
        ScriptedHttpAdapter(
            (HttpResponse("https://fixture.test/api/GetTrade/512867/0", 200, b"{}"),)
        ),
        MemoryEvidenceStore(),
        SourceSettings(),
    )
    with trace.activate():
        result = executor.execute(recipe, ExecutionContext(run_id=uuid4()))
    assert isinstance(result, CollectionRun)
    request = trace.report()["http"]["requests"][0]
    assert request["name"] == "Resource request 1"
    assert request["endpoint"] == "/api/GetTrade/{id}/0"


def test_trace_redacts_secret_like_path_segments() -> None:
    trace = PerformanceTrace()
    trace.http(
        label="GET",
        method="GET",
        url="https://fixture.test/api/token/opaque-secret-value",
        duration_ns=1_000_000,
        response_bytes=0,
        status=200,
        attempt=1,
        failed=False,
    )
    assert trace.report()["http"]["requests"][0]["endpoint"] == "/api/token/{redacted}"


def test_request_roles_use_recipe_metadata_and_labels_do_not_expose_paths() -> None:
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "unrelated-source",
            "version": "1",
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {
                        "url": "https://fixture.test/query",
                        "inventory_page": True,
                    },
                },
                {
                    "type": "http_request",
                    "parameters": {
                        "url": "https://fixture.test/token/opaque-secret-value",
                    },
                },
            ],
        }
    )
    trace = PerformanceTrace()
    executor = RecipeExecutor(
        ScriptedHttpAdapter(
            (
                HttpResponse("https://fixture.test/query", 200, b"{}"),
                HttpResponse("https://fixture.test/token/opaque-secret-value", 200, b"{}"),
            )
        ),
        MemoryEvidenceStore(),
        SourceSettings(),
    )
    with trace.activate():
        result = executor.execute(recipe, ExecutionContext(run_id=uuid4()))
    assert isinstance(result, CollectionRun)
    report = trace.report()
    requests = report["http"]["requests"]
    assert [r["name"] for r in requests] == ["Inventory page 1", "Resource request 1"]
    assert report["exclusive_timing_breakdown_ms"]["listing_http_ms"] == requests[0]["duration_ms"]
    assert report["exclusive_timing_breakdown_ms"]["detail_http_ms"] == requests[1]["duration_ms"]
    assert "opaque-secret-value" not in json.dumps(report)
