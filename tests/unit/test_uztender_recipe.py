"""Captured UZTender API responses verify source-supported recipe mappings offline."""

import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic import JsonValue

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import FileRecipeLoader, RecipeExecutor
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    BoundedCollectionResult,
    CollectionRun,
    ExecutionContext,
    LoopStep,
    SourceFailure,
    SourceRecipe,
    SourceRecord,
)
from tender_system.core.ports import HttpResponse
from tender_system.testing.fakes import ScriptedHttpAdapter


def test_document_metadata_maps_only_explicit_absolute_urls() -> None:
    raw: dict[str, JsonValue] = {
        "external_tender_id": "1",
        "documents": [
            {"url": "https://example.test/a.pdf"},
            "https://example.test/b.xlsx",
            {"url": "https://example.test/a.pdf"},
            {"name": "preview.pdf"},
            {"url": "/relative.pdf"},
            {"url": "file:///etc/passwd"},
        ],
    }
    result = ConfigurableTenderNormalizer().normalize(
        raw,
        SourceRecord(id="uztender", name="UZTender"),
        SourceSettings.model_validate(
            {
                "normalization": {
                    "field_mapping": {"document_urls": "documents"},
                    "parse_failure_policy": "null_with_issue",
                }
            }
        ),
    )
    assert result.candidate is not None
    assert result.candidate.document_urls == (
        "https://example.test/a.pdf",
        "https://example.test/b.xlsx",
    )
    assert result.candidate.raw_data == raw
    assert len(result.issues) == 3
    assert all(
        issue.field == "document_urls" and issue.severity == "warning" for issue in result.issues
    )
    strict = ConfigurableTenderNormalizer().normalize(
        raw,
        SourceRecord(id="uztender", name="UZTender"),
        SourceSettings.model_validate(
            {"normalization": {"field_mapping": {"document_urls": "documents"}}}
        ),
    )
    assert strict.candidate is None
    assert len(strict.issues) == 3


def test_captured_uztender_mapping_preserves_supported_fields_and_nested_products() -> None:
    fixture = json.loads(Path("tests/fixtures/uztender/captured-sample.json").read_text())
    recipe = FileRecipeLoader().load("tests/fixtures/uztender/recipe.json")
    replies = (
        fixture["listing"],
        fixture["detail"],
        {"data": fixture["additional_nested_detail"]},
    )
    http = ScriptedHttpAdapter(
        tuple(
            HttpResponse("https://api.uztender.com/api/catalog", 200, json.dumps(item).encode())
            for item in replies
        )
    )
    run = RecipeExecutor(http, MemoryEvidenceStore(), SourceSettings()).execute(
        recipe, ExecutionContext(run_id=uuid4())
    )

    assert isinstance(run, CollectionRun), (
        run.error_code,
        run.error_detail,
        run.failed_step,
    )
    assert run.complete and run.expected_count == 2
    assert recipe.version == "codex-2026-10-12-v2"
    assert len(run.raw_observations) == 2
    first, second = run.raw_observations
    assert first["source_url"] == (
        "https://uztender.com/tender/"
        "xiva-tumanidagi-birlashgan-kanalini-rekonstruktsiya-qilish-"
        "cmv57j5zv3hxvpv0nrqqm9515"
    )
    assert second["source_url"] == (
        "https://uztender.com/tender/mobilith-shc-100-moyi-cmv53ojk42xezpv0n5203bjb2"
    )
    assert first["external_tender_id"] == fixture["detail"]["data"]["id"]
    assert second["external_tender_id"] == fixture["additional_nested_detail"]["id"]
    assert first["currency"] == second["currency"] == "UZS"
    assert first["published_at"] is None
    assert second["published_at"] is None
    assert first["documents"] is None
    assert first["document_previews"] == fixture["detail"]["data"]["documentsPreview"]
    assert first["has_documents"] is True
    assert first["description"] is None
    assert first["summary"] == fixture["detail"]["data"]["summary"]
    assert first["lot_details"] is None
    assert second["lot_details"] == fixture["additional_nested_detail"]["lotDetails"]
    assert second["product_items"] is None

    candidates = [
        ConfigurableTenderNormalizer()
        .normalize(raw, SourceRecord(id="uztender", name="UZTender"), SourceSettings())
        .candidate
        for raw in run.raw_observations
    ]
    assert all(candidate is not None for candidate in candidates)
    assert candidates[0] is not None
    assert candidates[0].currency == "UZS"
    assert candidates[0].published_at is None
    assert candidates[0].document_urls == ()

    partial = RecipeExecutor(
        ScriptedHttpAdapter(
            tuple(
                HttpResponse("https://api.uztender.com/api/catalog", 200, json.dumps(item).encode())
                for item in replies[:2]
            )
        ),
        MemoryEvidenceStore(),
        SourceSettings(),
    ).execute(recipe, ExecutionContext(run_id=uuid4()), max_observations=1)
    assert isinstance(partial, BoundedCollectionResult)
    assert partial.stop_reason == "bounded_smoke_completed"
    assert len(partial.observations) == 1


def test_captured_uztender_pagination_mismatch_fails_closed() -> None:
    fixture = json.loads(Path("tests/fixtures/uztender/captured-sample.json").read_text())
    recipe = FileRecipeLoader().load("tests/fixtures/uztender/recipe.json")
    data = recipe.model_dump(mode="json")
    data["steps"][2]["parameters"]["max_iterations"] = 1
    mismatched = deepcopy(fixture["listing"])
    mismatched["meta"]["pagination"]["total"] = 3
    replies = (
        mismatched,
        fixture["detail"],
        {"data": fixture["additional_nested_detail"]},
    )
    execution = RecipeExecutor(
        ScriptedHttpAdapter(
            tuple(
                HttpResponse("https://api.uztender.com/api/catalog", 200, json.dumps(item).encode())
                for item in replies
            )
        ),
        MemoryEvidenceStore(),
        SourceSettings(),
    ).execute(SourceRecipe.model_validate(data), ExecutionContext(run_id=uuid4()))
    assert isinstance(execution, SourceFailure)
    assert execution.error_detail == "pagination/loop did not terminate within limit"
    assert len(execution.bad_output) == 2


def test_uztender_pilot_processes_one_page_of_at_most_100_incremental_records() -> None:
    fixture = json.loads(Path("tests/fixtures/uztender/captured-sample.json").read_text())
    recipe = FileRecipeLoader().load("tests/fixtures/uztender/pilot-100.json")
    rows = []
    details = []
    for index in range(105):
        tender_id = f"pilot-{index}"
        row = deepcopy(fixture["listing"]["data"][0])
        row.update(id=tender_id, slug=f"tender-{index}")
        rows.append(row)
        detail = deepcopy(fixture["detail"]["data"])
        detail["id"] = tender_id
        details.append({"data": detail})
    listing = {
        "data": rows,
        "meta": {"pagination": {"total": 10000, "offset": 0, "limit": 100}},
    }
    replies = (listing, *details)
    http = ScriptedHttpAdapter(
        tuple(
            HttpResponse("https://api.uztender.com/api/catalog", 200, json.dumps(item).encode())
            for item in replies
        )
    )

    run = RecipeExecutor(
        http, MemoryEvidenceStore(), SourceSettings(), sleep=lambda _: None
    ).execute(recipe, ExecutionContext(run_id=uuid4()))

    # Fail closed before processing if the endpoint ignores limit=100.
    assert isinstance(run, SourceFailure)
    assert run.error_detail == "loop exceeds iteration limit"
    assert len(http.requests) == 1
    assert parse_qs(urlsplit(http.requests[0].url).query) == {
        "limit": ["100"],
        "offset": ["0"],
    }


def test_full_capability_recipe_exhausts_source_and_preserves_original_files() -> None:
    fixture = json.loads(Path("tests/fixtures/uztender/captured-sample.json").read_text())
    recipe = FileRecipeLoader().load("recipes/uztender.json")
    original = FileRecipeLoader().load("tests/fixtures/uztender/recipe.json")
    assert isinstance(original.steps[2].parameters, LoopStep)
    assert isinstance(recipe.steps[2].parameters, LoopStep)
    assert original.steps[2].parameters.max_iterations == 100
    assert recipe.steps[2].parameters.max_iterations is None
    listing = deepcopy(fixture["listing"])
    listing["meta"]["pagination"]["total"] = 2
    terminal = deepcopy(listing)
    terminal["data"] = []
    detail = deepcopy(fixture["detail"])
    detail["data"]["documents"] = [{"url": "https://files.example.test/tender.pdf"}]
    replies = [listing, detail, {"data": fixture["additional_nested_detail"]}, terminal]
    adapter = ScriptedHttpAdapter(
        tuple(
            HttpResponse("https://api.uztender.com/api/catalog", 200, json.dumps(item).encode())
            for item in replies
        )
    )
    result = RecipeExecutor(adapter, MemoryEvidenceStore(), SourceSettings()).execute(
        recipe, ExecutionContext(run_id=uuid4())
    )
    assert isinstance(result, CollectionRun), result
    assert len(result.raw_observations) == 2 and len(adapter.requests) == 4
    assert result.diagnostics.completion is not None
    assert result.diagnostics.completion.kind == "empty_terminal_page"
    assert result.diagnostics.completion.observed
    assert result.diagnostics.inventory_totals == (2, 2)
    normalized = ConfigurableTenderNormalizer().normalize(
        result.raw_observations[0], SourceRecord(id="uztender", name="UZTender"), SourceSettings()
    )
    assert normalized.candidate is not None
    assert normalized.candidate.document_urls == ("https://files.example.test/tender.pdf",)


def test_actual_recipe_fetches_every_captured_page_and_preserves_records() -> None:
    fixture = json.loads(Path("tests/fixtures/uztender/captured-sample.json").read_text())
    recipe = FileRecipeLoader().load("tests/fixtures/uztender/recipe.json")
    data = recipe.model_dump(mode="json")
    loop = data["steps"][2]
    loop["steps"][0]["parameters"]["query"]["limit"] = 1
    offset_increment = next(
        step
        for step in loop["steps"]
        if step.get("type") == "transform" and step["parameters"].get("output") == "offset"
    )
    offset_increment["parameters"]["arguments"]["other"] = 1

    first_row, second_row = fixture["listing"]["data"]
    page_one = {
        "data": [first_row],
        "meta": {"pagination": {"total": 2, "offset": 0, "limit": 1}},
    }
    page_two = {
        "data": [second_row],
        "meta": {"pagination": {"total": 2, "offset": 1, "limit": 1}},
    }
    responses = (
        page_one,
        fixture["detail"],
        page_two,
        {"data": fixture["additional_nested_detail"]},
    )
    http = ScriptedHttpAdapter(
        tuple(
            HttpResponse("https://api.uztender.com/api/catalog", 200, json.dumps(item).encode())
            for item in responses
        )
    )
    run = RecipeExecutor(http, MemoryEvidenceStore(), SourceSettings()).execute(
        SourceRecipe.model_validate(data), ExecutionContext(run_id=uuid4())
    )
    assert isinstance(run, CollectionRun)
    assert run.complete and run.expected_count == 2
    assert [item["external_tender_id"] for item in run.raw_observations] == [
        first_row["id"],
        second_row["id"],
    ]
    assert len(http.requests) == 4
    assert parse_qs(urlsplit(http.requests[0].url).query) == {"limit": ["1"], "offset": ["0"]}
    assert urlsplit(http.requests[1].url).path.endswith(f"/catalog/{first_row['id']}")
    assert parse_qs(urlsplit(http.requests[2].url).query) == {"limit": ["1"], "offset": ["1"]}
    assert urlsplit(http.requests[3].url).path.endswith(f"/catalog/{second_row['id']}")
