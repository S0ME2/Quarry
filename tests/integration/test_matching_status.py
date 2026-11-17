"""Bounded current-state projection must never infer status from paginated history."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from tender_system.operator.web_matching import bounded_config
from tender_system.persistence.matching_storage import matching_planner
from tender_system.persistence.storage import MatchJobRow
from tender_system.testing.web import web_fixture

pytestmark = pytest.mark.database


def test_current_matching_projection_tracks_versions_and_pending_work(
    migrated_engine: Engine,
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        tender = client.post(
            "/api/v1/web/tenders", json={"request_id": str(uuid4()), "title": "Network router"}
        ).json()
        campaign = client.post(
            "/api/v1/web/campaigns", json={"criteria": {"name": "Network"}}
        ).json()
        body = {"campaign_id": campaign["id"], "tender_ids": [tender["id"]]}

        def status() -> dict[str, object]:
            response = client.post("/api/v1/web/matching/status", json=body)
            assert response.status_code == 200, response.text
            return dict(response.json()[0])

        assert status()["state"] == "not_evaluated"
        preview = client.post("/api/v1/web/matching/preview", json=body).json()
        assert client.post(f"/api/v1/web/matching/{preview['id']}/confirm").status_code == 202
        current = status()
        assert current["state"] == "current"
        assert current["result"] is not None
        updated = client.post(
            f"/api/v1/web/campaigns/{campaign['id']}/criteria",
            json={
                "criteria": {"name": "Changed requirements"},
                "expected_version_id": campaign["current_criteria_version_id"],
            },
        )
        assert updated.status_code == 200
        assert status()["state"] == "stale"
        preview = client.post("/api/v1/web/matching/preview", json=body).json()
        assert client.post(f"/api/v1/web/matching/{preview['id']}/confirm").status_code == 202
        assert status()["state"] == "current"
        with fixture.runtime.sessions.begin() as session:
            config = bounded_config(fixture.runtime.config, UUID(campaign["id"]), False, uuid4())
            matching_planner(session, config).for_pairs(
                ((UUID(tender["id"]), UUID(campaign["id"])),), fixture.runtime.clock.now()
            )
        assert status()["state"] == "pending"
        with fixture.runtime.sessions.begin() as session:
            job = session.scalar(select(MatchJobRow).where(MatchJobRow.status == "pending"))
            assert job is not None
            job.status, job.last_error_code = "failed", "provider_unavailable"
        assert status()["state"] == "failed"
        assert status()["error_code"] == "provider_unavailable"
        assert (
            client.post(
                "/api/v1/web/matching/status", json={**body, "tender_ids": [tender["id"]] * 101}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/web/matching/status", json={**body, "campaign_id": str(uuid4())}
            ).status_code
            == 404
        )
        assert fixture.calls == []
