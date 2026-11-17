"""Customer campaigns inherit policy unless a limit is explicitly configured."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tender_system.api.api import create_app
from tender_system.collection.validation_evidence import fingerprint
from tender_system.core.clock import SystemClock
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    OperationRow,
    RecipeRevisionRow,
    SourceRow,
    session_factory,
)

pytestmark = pytest.mark.database


def test_account_campaign_budget_inheritance_and_explicit_limits(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    with sessions.begin() as session:
        session.add(
            SourceRow(id="budget-source", name="Budget source", has_stable_external_id=True)
        )
        session.flush()
        session.add(
            RecipeRevisionRow(
                source_id="budget-source",
                version="1",
                revision={"recipe": {"version": "1"}},
                created_at=SystemClock().now(),
            )
        )
        session.flush()
        session.add(ActiveRecipeRow(source_id="budget-source", version="1"))
        session.add(
            OperationRow(
                id=uuid4(),
                kind="recipe_generation",
                source_id="budget-source",
                status="activated",
                payload={},
                created_at=SystemClock().now(),
                result={
                    "validated_recipe_fingerprint": fingerprint({"version": "1"}),
                    "validation_evidence_reference": "test-evidence",
                    "activation_operation_id": str(uuid4()),
                },
            )
        )
    with TestClient(create_app(sessions, operator_mode=False)) as client:
        credentials = {"email": "budget@example.com", "password": "test-password-long-enough"}
        assert client.post("/api/v1/auth/register", json=credentials).status_code == 201
        assert client.post("/api/v1/auth/login", json=credentials).status_code == 200
        criteria = {
            "name": "Business",
            "description": "network equipment",
            "source_ids": ["budget-source"],
            "notifications": {"enabled": False},
        }
        response = client.post("/api/v1/account/campaigns", json={"criteria": criteria})
        assert response.status_code == 201, response.text
        saved = response.json()
        assert saved["criteria"]["ai_budget_usd"] is None
        assert saved["criteria"]["ai_request_limit"] is None
        response = client.post(
            "/api/v1/account/campaigns",
            json={
                "criteria": {
                    **criteria,
                    "ai_budget_usd": 0.25,
                    "ai_request_limit": 3,
                }
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["criteria"]["ai_budget_usd"] == 0.25
        assert response.json()["criteria"]["ai_request_limit"] == 3
