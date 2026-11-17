"""Opt-in public downstream workflow; a fixture-installed recipe is NOT URL-only activation."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from tender_system.api.api import create_app
from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.core.configuration import ModelSettings, load_configuration
from tender_system.core.domain import ExecutionLimits
from tender_system.core.ports import ModelClient, ModelRequest, ModelResponse
from tender_system.matching.model_providers import model_client
from tender_system.operator.matching_budget import MatchingBudget
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    MatchResultRow,
    OperationRow,
    OperatorConfigRow,
    SourceStateRow,
    SqlTenderRepository,
    TelegramNotificationRow,
    TenderDocumentRow,
    TenderRow,
    TenderVersionRow,
    session_factory,
)
from tender_system.testing.fakes import FakeClock, FunctionModelClient

pytestmark = pytest.mark.database


@pytest.mark.skipif(
    os.environ.get("QUARRY_LIVE_GOLDEN") != "1", reason="Explicit public/provider opt-in required"
)
def test_existing_iom_recipe_scheduled_documents_matching_and_reuse(
    migrated_engine: Engine,
) -> None:
    config = load_configuration(validate_models=False)
    config.runtime.operator_worker_enabled = False
    config.runtime.manual_only = False
    config.runtime.operation_timeout_seconds = 300
    config.runtime.matching_enabled = True
    config.runtime.ai_semantic_enabled = True
    keyword = os.environ.get("QUARRY_IOM_KEYWORD", "solar-powered lighting")
    sample_size = int(os.environ.get("QUARRY_IOM_MAX_TENDERS", "3"))
    matching_mode = os.environ.get("QUARRY_IOM_MATCHING_MODE", "semantic")
    destination = Path("artifacts/tests/step5a/runs") / uuid4().hex
    catalog = destination / "catalog/iom"
    catalog.mkdir(parents=True)
    (catalog / "source.json").write_text(json.dumps({"id": "iom", "name": "IOM"}))
    asset = Path(".agents/skills/quarry-recipe-engineer/assets/iom.recipe.2026-10-19.json")
    (catalog / "recipe.json").write_text(asset.read_text())
    # Explicit sample-only policy: no freshness/full-inventory/deletion authority inferred.
    source_settings = config.global_settings.source.model_copy(
        update={
            "public_readonly": True,
            "calibration": config.global_settings.source.calibration.model_copy(
                update={
                    "full_inventory_supported": False,
                    "freshness": config.global_settings.source.calibration.freshness.model_copy(
                        update={"unknown_policy": "observe_only"}
                    ),
                }
            ),
        }
    )
    config = config.model_copy(
        update={
            "global_settings": config.global_settings.model_copy(update={"source": source_settings})
        }
    )
    sessions = session_factory(migrated_engine)
    evidence = MemoryEvidenceStore()
    clock = FakeClock(datetime.now(UTC))
    requests: list[ModelRequest] = []

    def models(settings: ModelSettings) -> ModelClient:
        delegate = model_client(settings)

        def generate(request: ModelRequest) -> ModelResponse:
            requests.append(request)
            return delegate.generate(request)

        return FunctionModelClient(generate)

    runtime = OperatorRuntime(sessions, config, evidence=evidence, clock=clock, models=models)
    summary: dict[str, Any] = {
        "url_only_activation": False,
        "recipe_setup": "fixture-installed existing IOM recipe",
        "runs": [],
        "live_telegram": os.environ.get("QUARRY_LIVE_TELEGRAM") == "1",
        "matching_mode": matching_mode,
    }
    try:
        runtime.register_catalog(destination / "catalog")
        limits = ExecutionLimits(
            max_tenders=sample_size,
            max_requests=40,
            max_duration_seconds=240,
            max_response_bytes=10_000_000,
            max_total_response_bytes=30_000_000,
        )
        with sessions.begin() as session:
            control = session.get(OperatorConfigRow, "source/iom")
            assert control is not None
            control.value = {
                **control.value,
                "manual_only": False,
                "approved_execution_limits": limits.model_dump(mode="json"),
            }
            session.add(
                OperatorConfigRow(
                    key="matching_budget",
                    updated_at=clock.now(),
                    value=MatchingBudget(
                        global_limit_usd=2,
                        unpriced_request_limit=6,
                        max_requests_per_run=6,
                        max_input_tokens=100000,
                        max_tokens_per_run=1000000,
                    ).model_dump(mode="json"),
                )
            )
        with TestClient(create_app(sessions, config, runtime=runtime)) as client:
            response = client.post(
                "/api/v1/web/campaigns",
                json={
                    "criteria": {
                        "name": f"IOM {keyword}",
                        "description": f"Procurement and supply of {keyword}."
                        if matching_mode == "semantic"
                        else "",
                        "include_keywords": [keyword],
                        "exclude_keywords": ["étude de faisabilité", "equipements mecaniques"],
                        "automatic_matching": True,
                        "automatic_mode": matching_mode,
                        "automatic_ai_authorized": True,
                        "ai_budget_usd": 2,
                        "ai_request_limit": 6,
                        "source_ids": ["iom"],
                        "notifications": {
                            "enabled": summary["live_telegram"],
                            "chat_id": os.environ.get("QUARRY_TELEGRAM_CHAT_ID") or None,
                        },
                    }
                },
            )
            assert response.status_code == 201, response.text
            counts = None
            for iteration in range(2):
                if iteration:
                    clock.instant += timedelta(seconds=source_settings.check_interval_seconds + 1)
                assert runtime.schedule_due() == 1
                with sessions() as session:
                    operation = session.scalar(
                        select(OperationRow).where(
                            OperationRow.kind == "collection", OperationRow.status == "queued"
                        )
                    )
                    assert operation is not None
                    key = operation.id
                assert runtime.tick(key)
                with sessions() as session:
                    operation = session.get(OperationRow, key)
                    assert operation is not None
                    summary["runs"].append(
                        {"id": str(key), "status": operation.status, "result": operation.result}
                    )
                    assert operation.status == "completed", operation.result
                    rows = list(session.scalars(select(TenderRow)))
                    documents = list(session.scalars(select(TenderDocumentRow)))
                    results = list(session.scalars(select(MatchResultRow)))
                    assert len(rows) == sample_size
                    assert sum(len(row.document_urls) for row in rows) == len(documents) > 0
                    assert any(
                        doc.status == "success" and len(doc.extracted_text or "") > 1000
                        for doc in documents
                    )
                    assert len(results) == sample_size
                    if matching_mode == "semantic":
                        assert requests and all(
                            cast(dict[str, Any], request.payload["tender"]).get("document_text")
                            for request in requests
                        )
                    else:
                        assert not requests
                    monitoring = cast(dict[str, Any], operation.result["monitoring"])
                    assert monitoring["found_tenders"] == sample_size
                    assert monitoring["status"] in {"completed", "partial_failure", "needs_review"}
                    state = session.get(SourceStateRow, "iom")
                    assert (
                        state is not None
                        and state.last_full_count is None
                        and state.checkpoint is None
                    )
                    current_counts = tuple(
                        session.scalar(select(func.count()).select_from(model))
                        for model in (TenderRow, TenderVersionRow, MatchResultRow)
                    )
                    if counts:
                        assert current_counts == counts
                        assert monitoring["documents_reused"] == len(documents)
                        assert monitoring["matching_results_reused"] == sample_size
                    counts = current_counts
                    summary["documents"] = [
                        {
                            "url": doc.url,
                            "status": doc.status,
                            "characters": len(doc.extracted_text or ""),
                            "error": doc.error,
                        }
                        for doc in documents
                    ]
                    summary["tenders"] = [
                        tender.model_dump(mode="json")
                        for row in rows
                        if (tender := SqlTenderRepository(session).get(row.id)) is not None
                    ]
                    summary["results"] = [row.outcome for row in results]
                    summary["model_requests"] = len(requests)
                    notifications = list(session.scalars(select(TelegramNotificationRow)))
                    summary["notifications"] = [
                        {"status": row.status, "attempts": row.attempts} for row in notifications
                    ]
                    if summary["live_telegram"]:
                        assert len(notifications) == monitoring["relevant_tenders"] == 1
                        assert notifications[0].status == "delivered"
                        assert notifications[0].attempts == 1
                        assert all(
                            label in notifications[0].message
                            for label in (
                                f"Campaign: IOM {keyword}",
                                "Tender:",
                                "Buyer:",
                                "Price:",
                                "Currency:",
                                "Deadline:",
                                "Platform: IOM",
                                "Relevance:",
                                "Link:",
                            )
                        )
            assert all(row["result"] in {"match", "no_match"} for row in summary["results"]), (
                summary["results"]
            )
    finally:
        with sessions() as session:
            summary["documents"] = [
                {
                    "url": doc.url,
                    "status": doc.status,
                    "characters": len(doc.extracted_text or ""),
                    "error": doc.error,
                }
                for doc in session.scalars(select(TenderDocumentRow))
            ]
            summary["results"] = [row.outcome for row in session.scalars(select(MatchResultRow))]
            summary["downstream"] = [
                {"status": row.status, "result": row.result, "payload": row.payload}
                for row in session.scalars(
                    select(OperationRow).where(OperationRow.kind == "automatic_matching")
                )
            ]
        (destination / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str)
        )
        (destination / "evidence.json").write_text(
            json.dumps(evidence._values, ensure_ascii=False, default=str)
        )
        (destination / "model-inputs.json").write_text(
            json.dumps([request.payload for request in requests], ensure_ascii=False, indent=2)
        )
        runtime.close()
