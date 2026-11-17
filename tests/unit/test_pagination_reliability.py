"""Pagination adversaries vary source behavior independently of executor arithmetic."""

import json
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.health import ConservativeRunHealth
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import RecipeExecutor
from tender_system.core.domain import (
    CollectionRun,
    ExecutionContext,
    ExecutionLimits,
    HealthDecision,
    SourceFailure,
    SourceRecipe,
    SourceRecord,
    SourceSnapshot,
)
from tender_system.core.ports import HttpRequest, HttpResponse
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import FunctionHttpAdapter


def page_recipe(*, reported: bool = True, snapshot: bool = False) -> SourceRecipe:
    spec: dict[str, JsonValue] = {
        "source_id": "pagination",
        "version": "1",
        "expected_count": {"$ref": "document.total"} if reported else None,
        "completion_signal": {"kind": "end_marker", "value": {"$ref": "done"}},
        "completion": {"$ref": "done"},
        "steps": [
            {
                "type": "transform",
                "parameters": {"name": "identity", "input": 0, "output": "offset"},
            },
            {
                "type": "loop",
                "parameters": {"until": {"$ref": "done"}},
                "steps": [
                    {
                        "type": "http_request",
                        "parameters": {
                            "url": "http://website.test/list",
                            "query": {"offset": {"$ref": "offset"}},
                            "output": "response",
                            "inventory_page": True,
                        },
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
                        "type": "extract",
                        "parameters": {
                            "format": "json",
                            "input": {"$ref": "document"},
                            "selector": "rows",
                            "emit": True,
                        },
                    },
                    {
                        "type": "transform",
                        "parameters": {
                            "name": "identity",
                            "input": {"$ref": "document.end"},
                            "output": "done",
                        },
                    },
                    {
                        "type": "transform",
                        "parameters": {
                            "name": "add",
                            "input": {"$ref": "offset"},
                            "arguments": {"other": 50},
                            "output": "offset",
                        },
                    },
                ],
            },
        ],
    }
    if snapshot:
        spec["inventory_snapshot"] = {"$ref": "document.snapshot"}
    return SourceRecipe.model_validate(spec)


def collect(
    count: int,
    limit: int | None = None,
    *,
    reported: bool = True,
    scenario: str = "stable",
    snapshot: bool = False,
    max_requests: int = 1000,
) -> tuple[CollectionRun | SourceFailure, int]:
    requests = 0

    def respond(request: HttpRequest) -> HttpResponse:
        nonlocal requests
        requests += 1
        offset = int(parse_qs(urlsplit(request.url).query)["offset"][0])
        if scenario == "repeat_page":
            offset = 0
        rows: list[JsonValue] = [
            {"external_tender_id": str(i), "title": f"Supply {i}"}
            for i in range(offset, min(offset + 50, count))
        ]
        if scenario == "duplicate" and offset == 50 and rows:
            rows[0] = {"external_tender_id": "49", "title": "Supply 49"}
        if scenario == "shift" and offset >= 50:
            rows = [
                {"external_tender_id": str(i - 1), "title": f"Supply {i - 1}"}
                for i in range(offset, min(offset + 50, count))
            ]
        total = count
        if scenario == "wrong_total":
            total = count - 1
        if scenario in {"increase", "decrease"} and offset >= 50:
            total += 1 if scenario == "increase" else -1
        body = {
            "rows": rows,
            "total": total,
            "end": offset + 50 >= count,
            "snapshot": "changed" if scenario == "snapshot_change" and offset else "stable",
        }
        return HttpResponse(request.url, 200, json.dumps(body).encode())

    policy = fixture_source_settings()
    result = RecipeExecutor(FunctionHttpAdapter(respond), MemoryEvidenceStore(), policy).execute(
        page_recipe(reported=reported, snapshot=snapshot),
        ExecutionContext(
            run_id=uuid4(), limits=ExecutionLimits(max_tenders=limit, max_requests=max_requests)
        ),
    )
    return result, requests


def health(run: CollectionRun) -> HealthDecision:
    policy = fixture_source_settings()
    normalizer = ConfigurableTenderNormalizer()
    source = SourceRecord(id="pagination", name="Local pagination")
    candidates = tuple(
        normalizer.normalize(raw, source, policy).candidate for raw in run.raw_observations
    )
    assert all(candidates)
    return ConservativeRunHealth().evaluate(
        run.model_copy(update={"candidates": candidates}), policy, SourceSnapshot(), ()
    )


@pytest.mark.parametrize("count", [0, 1, 100, 101, 250, 2980, 12001])
@pytest.mark.parametrize("limit", [None, 100, 500])
def test_same_recipe_supports_arbitrary_full_or_bounded_scope(
    count: int, limit: int | None
) -> None:
    result, requests = collect(count, limit)
    assert isinstance(result, CollectionRun), result
    assert len(result.raw_observations) == (min(count, limit) if limit else count)
    assert len({raw["external_tender_id"] for raw in result.raw_observations}) == len(
        result.raw_observations
    )
    assert requests <= max(1, (min(count, limit) if limit else count) // 50 + 1)
    if limit:
        assert result.coverage == "sampled" and not result.complete
    else:
        assert result.complete


@pytest.mark.parametrize("scenario", ["increase", "decrease", "snapshot_change"])
def test_changing_inventory_cannot_claim_full_authority(scenario: str) -> None:
    result, _ = collect(250, scenario=scenario, snapshot=True)
    assert isinstance(result, CollectionRun)
    decision = health(result)
    assert not decision.publication_allowed
    assert not decision.absence_evidence_allowed


def test_stable_total_without_snapshot_does_not_authorize_mutable_pagination_absence() -> None:
    result, _ = collect(250)
    assert isinstance(result, CollectionRun)
    decision = health(result)
    assert decision.publication_allowed
    assert not decision.absence_evidence_allowed


@pytest.mark.parametrize("scenario", ["duplicate", "shift", "wrong_total"])
def test_inconsistent_source_cannot_publish_full_inventory(scenario: str) -> None:
    result, _ = collect(250, scenario=scenario)
    assert isinstance(result, CollectionRun)
    assert not health(result).publication_allowed


def test_repeated_inventory_response_stops_before_request_budget() -> None:
    result, requests = collect(250, scenario="repeat_page", max_requests=10)
    assert isinstance(result, SourceFailure)
    assert requests == 2
    assert "repeated" in result.error_detail.lower()


def test_missing_total_can_exhaust_from_real_terminal_marker() -> None:
    result, _ = collect(250, reported=False, snapshot=True)
    assert isinstance(result, CollectionRun) and result.expected_count is None
    assert health(result).publication_allowed


@settings(max_examples=40, deadline=None)
@given(
    count=st.integers(min_value=1, max_value=700),
    limit=st.one_of(st.none(), st.integers(min_value=1, max_value=900)),
)
def test_random_scope_never_loses_or_duplicates_records(count: int, limit: int | None) -> None:
    result, _ = collect(count, limit)
    assert isinstance(result, CollectionRun)
    expected = min(count, limit) if limit else count
    assert len(result.raw_observations) == expected
    assert len({row["external_tender_id"] for row in result.raw_observations}) == expected
    if limit and count >= limit:
        assert result.coverage == "sampled" and not result.complete


def test_bounded_scope_can_exceed_ten_thousand_when_budget_allows() -> None:
    limits = ExecutionLimits(max_tenders=12000, max_requests=1000)
    result, _ = collect(12001, limits.max_tenders)
    assert isinstance(result, CollectionRun)
    assert len(result.raw_observations) == 12000 and result.coverage == "sampled"


@pytest.mark.parametrize("mode", ["page", "cursor", "link", "post"])
@pytest.mark.parametrize("limit", [None, 100])
def test_declarative_page_cursor_link_and_post_pagination(mode: str, limit: int | None) -> None:
    data = page_recipe().model_dump(mode="json")
    initial: JsonValue = "http://website.test/list?offset=0" if mode == "link" else 0
    data["steps"][0]["parameters"]["input"] = initial
    body = data["steps"][1]["steps"]
    http = body[0]["parameters"]
    if mode == "link":
        http["url"], http["query"] = {"$ref": "offset"}, {}
    elif mode == "post":
        http["method"], http["query"], http["body"] = "POST", {}, {"cursor": {"$ref": "offset"}}
    else:
        http["query"] = {mode: {"$ref": "offset"}}
    body[-1]["parameters"] = {
        "name": "identity",
        "input": {"$ref": "document.next"},
        "output": "offset",
    }
    requests = 0

    def respond(request: HttpRequest) -> HttpResponse:
        nonlocal requests
        requests += 1
        query = parse_qs(urlsplit(request.url).query)
        if mode == "post":
            assert request.body is not None
            offset = int(json.loads(request.body)["cursor"])
        elif mode == "link":
            offset = int(query["offset"][0])
        else:
            token = int(query[mode][0])
            offset = token * 50 if mode == "page" else token
        next_offset = offset + 50
        next_value: JsonValue = next_offset // 50 if mode == "page" else next_offset
        if mode == "link":
            next_value = f"http://website.test/list?offset={next_offset}"
        payload = {
            "rows": [
                {"external_tender_id": str(i), "title": f"Supply {i}"}
                for i in range(offset, min(offset + 50, 250))
            ],
            "next": next_value,
            "total": 250,
            "end": next_offset >= 250,
        }
        return HttpResponse(request.url, 200, json.dumps(payload).encode())

    result = RecipeExecutor(
        FunctionHttpAdapter(respond), MemoryEvidenceStore(), fixture_source_settings()
    ).execute(
        SourceRecipe.model_validate(data),
        ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_tenders=limit)),
    )
    assert isinstance(result, CollectionRun), result
    assert len(result.raw_observations) == (limit or 250)
    assert requests == (2 if limit else 5)


def test_delayed_scope_count_does_not_fabricate_stable_global_total_evidence() -> None:
    recipe = page_recipe(snapshot=True).model_dump(mode="json")
    recipe["expected_count"] = {"$ref": "late_total"}
    recipe["steps"].append(
        {
            "type": "transform",
            "parameters": {
                "name": "identity",
                "input": {"$ref": "document.total"},
                "output": "late_total",
            },
        }
    )

    def respond(request: HttpRequest) -> HttpResponse:
        offset = int(parse_qs(urlsplit(request.url).query)["offset"][0])
        payload = {
            "rows": [
                {"external_tender_id": str(i), "title": f"Supply {i}"}
                for i in range(offset, min(offset + 50, 250))
            ],
            "total": 249 if offset == 0 else 250,
            "snapshot": "claims-stable",
            "end": offset + 50 >= 250,
        }
        return HttpResponse(request.url, 200, json.dumps(payload).encode())

    executor = RecipeExecutor(
        FunctionHttpAdapter(respond), MemoryEvidenceStore(), fixture_source_settings()
    )
    result = executor.execute(SourceRecipe.model_validate(recipe), ExecutionContext(run_id=uuid4()))
    assert isinstance(result, CollectionRun)
    decision = health(result)
    assert decision.publication_allowed and not decision.absence_evidence_allowed
    assert "inventory_total_unobserved" in {r.code for r in decision.reasons}
    recipe["inventory_total"] = {"$ref": "document.total"}
    observed = executor.execute(
        SourceRecipe.model_validate(recipe), ExecutionContext(run_id=uuid4())
    )
    assert isinstance(observed, CollectionRun)
    assert not health(observed).publication_allowed
    assert set(observed.diagnostics.inventory_totals) == {249, 250}


def test_bounded_run_preserves_declared_inventory_timestamp_without_publication_fabrication() -> (
    None
):
    recipe = page_recipe().model_copy(update={"data_as_of": {"$ref": "document.as_of"}})

    def respond(request: HttpRequest) -> HttpResponse:
        return HttpResponse(
            request.url,
            200,
            json.dumps(
                {
                    "rows": [{"external_tender_id": "1", "title": "Supply"}],
                    "total": 250,
                    "as_of": "2026-10-13T12:00:00Z",
                    "end": False,
                }
            ).encode(),
        )

    result = RecipeExecutor(
        FunctionHttpAdapter(respond), MemoryEvidenceStore(), fixture_source_settings()
    ).execute(recipe, ExecutionContext(run_id=uuid4(), limits=ExecutionLimits(max_tenders=1)))
    assert isinstance(result, CollectionRun)
    assert result.data_as_of == datetime.fromisoformat("2026-10-13T12:00:00+00:00")
    candidate = (
        ConfigurableTenderNormalizer()
        .normalize(
            result.raw_observations[0],
            SourceRecord(id="pagination", name="Source"),
            fixture_source_settings(),
        )
        .candidate
    )
    assert candidate is not None and candidate.published_at is None
