"""Opt-in public URL-only onboarding through the production activation boundary."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tender_system.api.api import create_app
from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.core.configuration import load_configuration
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    OperatorConfigRow,
    SourceRow,
    TenderRow,
    session_factory,
)

pytestmark = [pytest.mark.database, pytest.mark.browser]


@pytest.mark.skipif(
    os.environ.get("QUARRY_LIVE_GOLDEN") != "1" or not os.environ.get("QUARRY_PUBLIC_URL"),
    reason="Explicit live opt-in and QUARRY_PUBLIC_URL required",
)
def test_public_url_only_onboarding(migrated_engine: Engine) -> None:
    config = load_configuration(validate_models=False)
    config.runtime.operator_worker_enabled = False
    config.runtime.operation_timeout_seconds = 360
    base_model = config.global_settings.source.fixer_model
    output_tokens = base_model.max_output_tokens or 8000
    if base_model.provider == "unconfigured":
        base_model = config.for_campaign("onboarding").model
        output_tokens = 8000
    model = base_model.model_copy(
        update={
            "max_output_tokens": output_tokens,
            "max_attempts": 2,
            "timeout_seconds": 90,
            "request_interval_seconds": max(base_model.request_interval_seconds, 61)
            if base_model.provider == "groq"
            else base_model.request_interval_seconds,
        }
    )
    source = config.global_settings.source.model_copy(
        update={
            "fixer_model": model,
            "investigation": config.global_settings.source.investigation.model_copy(
                update={"timeout_seconds": 300}
            ),
        }
    )
    config = config.model_copy(
        update={"global_settings": config.global_settings.model_copy(update={"source": source})}
    )
    sessions = session_factory(migrated_engine)
    evidence = MemoryEvidenceStore()
    runtime = OperatorRuntime(sessions, config, evidence=evidence)
    destination = Path("artifacts/tests/step5/public") / uuid4().hex
    destination.mkdir(parents=True)
    try:
        with TestClient(create_app(sessions, config, runtime=runtime)) as client:
            response = client.post(
                "/api/v1/web/sources", json={"url": os.environ["QUARRY_PUBLIC_URL"]}
            )
            assert response.status_code == 201
            key = UUID(response.json()["operation_id"])
            source_id = response.json()["source_id"]
            with sessions() as session:
                operation = session.get(OperationRow, key)
                assert operation is not None
                result = {
                    "url": os.environ["QUARRY_PUBLIC_URL"],
                    "provider": model.provider,
                    "model": model.model,
                    "max_output_tokens": model.max_output_tokens,
                    "status": operation.status,
                    "error_code": operation.error_code,
                    "result": operation.result,
                }
                (destination / "result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2)
                )
                (destination / "evidence.json").write_text(
                    json.dumps(evidence._values, ensure_ascii=False)
                )
                assert operation.status == "activated", (
                    f"Public onboarding failed; evidence: {destination}"
                )
                assert operation.result["validation_passed"] is True
                assert operation.result["replay_passed"] is True
                verification = operation.result["verification"]
                assert isinstance(verification, dict) and int(str(verification["checked"])) >= 1
                assert session.get(SourceRow, source_id)
                assert session.get(ActiveRecipeRow, source_id)
                control = session.get(OperatorConfigRow, "source/" + source_id)
                assert control and control.value["manual_only"] is False
            response = client.post(
                f"/api/v1/web/sources/{source_id}/collect",
                json={"max_tenders": 3, "max_requests": 100, "max_duration_seconds": 300},
            )
            assert response.status_code == 202, response.text
            with sessions() as session:
                from sqlalchemy import select

                collection = session.get(OperationRow, UUID(response.json()["id"]))
                assert collection is not None
                (destination / "collection.json").write_text(
                    json.dumps(
                        {"status": collection.status, "result": collection.result},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                assert collection.status == "completed", collection.result
                assert session.scalar(select(TenderRow.id).where(TenderRow.source_id == source_id))
    finally:
        runtime.close()
