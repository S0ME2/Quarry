import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.recipes import FileRecipeLoader
from tender_system.core.configuration import Configuration, SourceSettings
from tender_system.core.domain import ExecutionLimits, FixResult, SourceRecipe
from tender_system.core.execution import execution_budget
from tender_system.core.ports import HttpResponse
from tender_system.operator.operations import OperationRestriction
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.testing.fakes import FakeBrowserAdapter, FakePage, ScriptedHttpAdapter


class EmptySession:
    def __enter__(self) -> "EmptySession":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, *_: object) -> None:
        return None


def test_inherited_validation_scope_is_bound_to_evidence_and_approval_contract() -> None:
    url = "https://fixture.test/tender/123"
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "fixture",
            "version": "1",
            "steps": [
                {
                    "type": "transform",
                    "parameters": {
                        "name": "identity",
                        "emit": True,
                        "input": [
                            {
                                "external_tender_id": str(i),
                                "title": "Equipment",
                                "source_url": f"{url}/{i}",
                            }
                            for i in range(3)
                        ],
                    },
                }
            ],
        }
    )
    evidence = MemoryEvidenceStore()
    runtime = OperatorRuntime(
        lambda: EmptySession(),  # type: ignore[arg-type]
        Configuration(),
        evidence=evidence,
        http=ScriptedHttpAdapter(()),
        browsers=lambda: FakeBrowserAdapter(
            {f"{url}/{i}": FakePage("<h1>Equipment</h1>") for i in range(3)}
        ),
    )
    limits = ExecutionLimits(max_tenders=2)
    try:
        with execution_budget(10, limits=limits):
            result = runtime.test_proposal(
                uuid4(),
                "fixture",
                FixResult(
                    status="fixed",
                    confidence=1,
                    explanation="Inherited scope fixture",
                    updated_recipe=recipe,
                ),
                SourceSettings(),
            )
        assert result["validation_passed"] is True, result
        assert result["execution_limits"] == limits.model_dump(mode="json")
        capture = evidence.get(str(result["validation_evidence_reference"]))
        assert capture["execution_limits"] == result["execution_limits"]
        health = result["health"]
        assert isinstance(health, dict)
        assert health["inventory_mode"] == "sampled"
        assert health["absence_evidence_allowed"] is False
    finally:
        runtime.close()


def test_replay_mismatch_cannot_authorize_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://fixture.test/tender/123"
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "fixture",
            "version": "1",
            "coverage": "incremental",
            "expected_count": 1,
            "steps": [
                {
                    "type": "transform",
                    "parameters": {
                        "name": "identity",
                        "emit": True,
                        "input": {
                            "external_tender_id": "123",
                            "title": "Equipment",
                            "source_url": url,
                        },
                    },
                }
            ],
        }
    )
    runtime = OperatorRuntime(
        lambda: EmptySession(),  # type: ignore[arg-type]
        Configuration(),
        evidence=MemoryEvidenceStore(),
        http=ScriptedHttpAdapter(()),
        browsers=lambda: FakeBrowserAdapter({url: FakePage("<h1>Equipment</h1>")}),
    )
    original = runtime.normalizer.normalize
    calls = 0

    def drifting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls > 1 and result.candidate:
            result = result.model_copy(
                update={"candidate": result.candidate.model_copy(update={"title": "Changed"})}
            )
        return result

    monkeypatch.setattr(runtime.normalizer, "normalize", drifting)
    try:
        result = runtime.test_proposal(
            uuid4(),
            "fixture",
            FixResult(
                status="fixed",
                confidence=1,
                explanation="Replay contract fixture",
                updated_recipe=recipe,
            ),
            SourceSettings(),
        )
        assert result["validation_passed"] is False and result["replay_passed"] is False
        assert result["error_code"] == "replay_mismatch"
        assert result["validation_evidence_reference"]
    finally:
        runtime.close()


def test_recipe_validation_http_timeout_surfaces_step_and_evidence_offline() -> None:
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "fixture",
            "version": "1",
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {"url": "https://fixture.test/api", "output": "page"},
                }
            ],
        }
    )
    proposal = FixResult(
        status="fixed",
        updated_recipe=recipe,
        explanation="fixture recipe",
        confidence=1,
    )
    evidence = MemoryEvidenceStore()
    runtime = OperatorRuntime(
        lambda: EmptySession(),  # type: ignore[arg-type]
        Configuration(),
        evidence=evidence,
        http=ScriptedHttpAdapter((TimeoutError(),) * 3),
    )
    settings = (
        Configuration()
        .for_source("fixture")
        .model_copy(
            update={
                "retry": Configuration()
                .for_source("fixture")
                .retry.model_copy(update={"backoff_seconds": 0.001})
            }
        )
    )

    result = cast(dict[str, Any], runtime.test_proposal(uuid4(), "fixture", proposal, settings))

    assert result["validation_passed"] is False
    assert result["failed_step"] == "0:http_request"
    assert result["evidence_references"] == [result["evidence_reference"]]
    assert "timed out at 0:http_request" in result["warnings"][0]
    assert evidence.get(result["evidence_reference"])["error_type"] == "TimeoutError"
    runtime.http = ScriptedHttpAdapter((TimeoutError(),) * 3)
    with pytest.raises(OperationRestriction, match="TimeoutError.*0:http_request") as failure:
        runtime.fresh_source_check(uuid4(), "fixture", recipe, settings, {})
    assert failure.value.stage == "approval_fresh_check"
    assert evidence.get(failure.value.evidence_references[0])["error_type"] == "TimeoutError"
    runtime.close()


def test_incremental_recipe_validation_replays_and_allows_observed_records_offline() -> None:
    detail_url = "https://fixture.test/tender/1"
    recipe = SourceRecipe.model_validate(
        {
            "source_id": "fixture",
            "version": "1",
            "coverage": "incremental",
            "completion": True,
            "expected_count": {"$ref": "record_count"},
            "steps": [
                {
                    "type": "http_request",
                    "parameters": {"url": "https://fixture.test/api", "output": "page"},
                },
                {
                    "type": "extract",
                    "parameters": {
                        "input": {"$ref": "page.body"},
                        "format": "json",
                        "selector": "data",
                        "fields": {
                            "external_tender_id": {"selector": "id", "required": True},
                            "title": {"selector": "title", "required": True},
                            "source_url": {"selector": "url", "required": True},
                        },
                        "output": "records",
                        "emit": True,
                    },
                },
                {
                    "type": "transform",
                    "parameters": {
                        "name": "length",
                        "input": {"$ref": "records"},
                        "output": "record_count",
                    },
                },
            ],
        }
    )
    proposal = FixResult(
        status="fixed",
        updated_recipe=recipe,
        explanation="bounded fixture recipe",
        confidence=1,
    )
    body = json.dumps(
        {"data": [{"id": "1", "title": "Fixture tender", "url": detail_url}]}
    ).encode()
    evidence = MemoryEvidenceStore()
    runtime = OperatorRuntime(
        lambda: EmptySession(),  # type: ignore[arg-type]
        Configuration(),
        evidence=evidence,
        http=ScriptedHttpAdapter((HttpResponse("https://fixture.test/api", 200, body),)),
        browsers=lambda: FakeBrowserAdapter({detail_url: FakePage("<h1>Fixture tender</h1>")}),
    )

    result = cast(
        dict[str, Any], runtime.test_proposal(uuid4(), "fixture", proposal, SourceSettings())
    )

    assert result["validation_passed"] is True
    assert result["replay_passed"] is True
    health = result["health"]
    assert health["publication_allowed"] is True
    assert health["inventory_mode"] == "incremental"
    assert health["dimensions"]["inventory"] == "unknown"
    assert health["freshness"]["status"] == "unknown"
    assert health["freshness"]["timestamp"] is None
    assert health["absence_evidence_allowed"] is False
    runtime.close()


def test_uztender_pilot_validates_and_replays_100_records_offline() -> None:
    fixture = json.loads(Path("tests/fixtures/uztender/captured-sample.json").read_text())
    recipe = FileRecipeLoader().load("tests/fixtures/uztender/pilot-100.json")
    listing_rows = []
    details = []
    pages = {}
    for index in range(100):
        tender_id = f"pilot-{index}"
        title = f"Pilot tender {index}"
        row = deepcopy(fixture["listing"]["data"][0])
        row.update(id=tender_id, slug=f"pilot-{index}")
        listing_rows.append(row)
        detail = deepcopy(fixture["detail"]["data"])
        detail.update(id=tender_id, title=title)
        details.append({"data": detail})
        source_url = f"https://uztender.com/tender/pilot-{index}-{tender_id}"
        pages[source_url] = FakePage(f"<h1>{title}</h1>")
    listing = {
        "data": listing_rows,
        "meta": {"pagination": {"total": 10000, "offset": 0, "limit": 100}},
    }
    payloads = (listing, *details)
    http = ScriptedHttpAdapter(
        tuple(
            HttpResponse("https://api.uztender.com/api/catalog", 200, json.dumps(item).encode())
            for item in payloads
        )
    )
    runtime = OperatorRuntime(
        lambda: EmptySession(),  # type: ignore[arg-type]
        Configuration(),
        evidence=MemoryEvidenceStore(),
        http=http,
        browsers=lambda: FakeBrowserAdapter(pages),
    )
    proposal = FixResult(
        status="fixed",
        updated_recipe=recipe,
        explanation="bounded UZTender pilot",
        confidence=1,
    )

    result = cast(
        dict[str, Any], runtime.test_proposal(uuid4(), recipe.source_id, proposal, SourceSettings())
    )

    assert result["validation_passed"] is True
    assert result["replay_passed"] is True
    assert result["expected_count"] == 100
    assert len(http.requests) == 101
    assert result["health"]["publication_allowed"] is True
    assert result["health"]["inventory_mode"] == "incremental"
    assert result["health"]["absence_evidence_allowed"] is False
    assert result["health"]["freshness"]["status"] == "unknown"
    assert result["health"]["freshness"]["timestamp"] is None
    runtime.close()
