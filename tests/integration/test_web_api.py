"""Real operator HTTP services and PostgreSQL; owned website and mock AI only."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tender_system.persistence.storage import MatchJobRow, session_factory
from tender_system.testing.web import web_fixture

pytestmark = pytest.mark.database


@pytest.mark.parametrize("mode", ["rules", "semantic"])
def test_large_manual_selection_reuses_and_forces_only_selected_pairs(
    migrated_engine: Engine, mode: str
) -> None:
    from tender_system.core.configuration import Configuration
    from tender_system.testing.calibration import fixture_source_settings

    config = Configuration.model_validate(
        {
            "global": {
                "source": fixture_source_settings().model_dump(mode="json"),
                "campaign": {"batch_size": 2, "timeout_seconds": 1, "lease_seconds": 100},
            },
            "runtime": {
                "matching_enabled": True,
                "ai_semantic_enabled": True,
                "operator_worker_enabled": False,
                "manual_only": True,
            },
        }
    )
    with web_fixture(migrated_engine, config) as fixture, TestClient(fixture.app) as client:
        ids = []
        for index in range(14):
            tender = client.post(
                "/api/v1/web/tenders",
                json={
                    "request_id": str(uuid4()),
                    "title": f"Network router {index}",
                    "product_items": [{"name": "Network router"}],
                },
            )
            assert tender.status_code == 201, tender.text
            ids.append(tender.json()["id"])
        campaign = client.post(
            "/api/v1/web/campaigns",
            json={
                "criteria": {
                    "ai_budget_usd": 10,
                    "name": "Network",
                    "include_keywords": ["network"],
                }
            },
        ).json()
        body = {"campaign_id": campaign["id"], "tender_ids": ids[:13], "mode": mode}

        def run(force: bool = False) -> dict[str, object]:
            preview = client.post("/api/v1/web/matching/preview", json={**body, "force_new": force})
            assert preview.status_code == 201, preview.text
            key = preview.json()["id"]
            confirmed = client.post(f"/api/v1/web/matching/{key}/confirm", json={})
            assert confirmed.status_code == 202, confirmed.text
            operation = client.get(f"/api/v1/web/operations/{key}").json()
            assert operation["status"] == "completed", operation
            return dict(operation["result"])

        first = run()
        assert first["completed"] == 13
        assert len(fixture.calls) == (13 if mode == "semantic" else 0)
        second = run()
        assert second["claimed"] == 0
        assert len(fixture.calls) == (13 if mode == "semantic" else 0)
        assert run(True)["completed"] == 13
        assert len(fixture.calls) == (26 if mode == "semantic" else 0)
        results = client.get("/api/v1/web/results", params={"campaign_id": campaign["id"]}).json()
        assert results["total"] == 26
        assert {r["tender_id"] for r in results["items"]} == set(ids[:13])
        assert (
            client.post(
                "/api/v1/web/matching/preview", json={**body, "tender_ids": [ids[0]] * 501}
            ).status_code
            == 422
        )


def add_source(client: TestClient, url: str) -> str:
    response = client.post("/api/v1/web/sources", json={"url": url})
    assert response.status_code == 201, response.text
    return str(response.json()["source_id"])


def save_recipe(client: TestClient, key: str, text: str, **bounds: object) -> dict[str, object]:
    response = client.post(
        f"/api/v1/web/sources/{key}/recipe", json={"text": text, "max_tenders": 2, **bounds}
    )
    assert response.status_code == 202, response.text
    return dict(client.get("/api/v1/web/operations/" + response.json()["id"]).json())


def test_single_operator_workflow(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        assert fixture.runtime.thread is None
        assert not fixture.runtime.config.runtime.operator_worker_enabled
        assert fixture.runtime.config.runtime.manual_only
        assert client.get("/api/v1/web/sources").status_code == 200
        assert (
            client.post(
                "/api/v1/web/sources", headers={"Origin": "https://external.example"}, json={}
            ).status_code
            == 403
        )

        def post(path: str, body: object, code: int = 201) -> object:
            response = client.post("/api/v1" + path, json=body)
            assert response.status_code == code, response.text
            return response.json()

        source_id = add_source(client, fixture.source_url)
        invalid = client.post(f"/api/v1/web/sources/{source_id}/recipe", json={"text": "{}"})
        assert invalid.status_code == 422 and "steps" in invalid.text
        assert invalid.json()["error"]["fields"][0]["field"] == "steps"
        setup = save_recipe(client, source_id, fixture.recipe)
        assert setup["status"] == "activated", setup
        assert (
            client.post(
                f"/api/v1/web/sources/{source_id}/collect", json={"max_tenders": 3}
            ).status_code
            == 202
        )
        collection = post(f"/web/sources/{source_id}/collect", {"max_tenders": 2}, 202)
        assert isinstance(collection, dict)
        collected = client.get(f"/api/v1/web/operations/{collection['id']}").json()
        assert collected["status"] == "completed", collected
        catalog = client.get(f"/api/v1/web/tenders?q=Network&source={source_id}").json()
        assert catalog["total"] == 2
        assert client.get("/api/v1/web/tenders?buyer=procurement&currency=USD").json()["total"] == 2
        assert client.get("/api/v1/web/tenders?currency=UZS").json()["total"] == 0
        tender = catalog["items"][0]
        detail = client.get("/api/v1/web/tenders/" + tender["id"]).json()
        assert detail["product_items"][0]["name"] == "Network router"
        manual = post(
            "/web/tenders",
            {
                "request_id": str(uuid4()),
                "title": "Manual router",
                "value": "100",
                "currency": "USD",
                "deadline": "2026-12-01T00:00:00Z",
                "product_items": [{"name": "Network router"}],
            },
        )
        assert isinstance(manual, dict)
        assert client.get("/api/v1/web/tenders/" + manual["id"]).json()["provenance"] == "manual"
        campaign = post(
            "/web/campaigns",
            {"criteria": {"ai_budget_usd": 10, "name": "Network", "include_keywords": ["network"]}},
        )
        assert isinstance(campaign, dict)
        from sqlalchemy import select

        from tender_system.persistence.storage import (
            OperationRow,
            OperatorConfigRow,
        )

        with session_factory(migrated_engine)() as session:
            assert "actor_id" not in OperatorConfigRow.__table__.columns
            assert "actor_id" not in OperationRow.__table__.columns

        with session_factory(migrated_engine)() as session:
            assert list(session.scalars(select(MatchJobRow))) == []
        preview = post(
            "/web/matching/preview",
            {"campaign_id": campaign["id"], "tender_ids": [tender["id"]], "mode": "rules"},
        )
        assert isinstance(preview, dict)
        post(f"/web/matching/{preview['id']}/confirm", {}, 202)
        assert fixture.calls == []
        ai = post(
            "/web/matching/preview",
            {"campaign_id": campaign["id"], "tender_ids": [tender["id"]], "mode": "semantic"},
        )
        assert isinstance(ai, dict) and ai["result"]["maximum_model_requests"] == 1
        assert fixture.calls == []
        post(f"/web/matching/{ai['id']}/confirm", {}, 202)
        assert len(fixture.calls) == 1, json.dumps(
            client.get(f"/api/v1/web/operations/{ai['id']}").json()
        )
        assert client.post(f"/api/v1/web/matching/{ai['id']}/confirm", json={}).status_code == 409
        results = client.get("/api/v1/web/results", params={"campaign_id": campaign["id"]}).json()[
            "items"
        ]
        semantic = next(r for r in results if r["matcher_implementation"] == "intelligent")
        assert semantic["outcome"]["model_opinion"]["decision"] == "match"
        assert semantic["outcome"]["evidence_verification"]["status"] == "verified"
        assert client.get("/api/v1/web/operations").json()


@pytest.mark.parametrize(
    "behavior", ["unavailable", "invalid_response", "invalid_evidence", "unsure"]
)
def test_semantic_failures_and_uncertainty_are_distinct(
    migrated_engine: Engine, behavior: str
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        fixture.model_behavior = behavior
        tender = client.post(
            "/api/v1/web/tenders",
            json={
                "request_id": str(uuid4()),
                "title": "Network equipment",
                "value": "100",
                "currency": "USD",
                "deadline": "2026-12-01T00:00:00Z",
                "product_items": [{"name": "Network router"}],
            },
        ).json()
        campaign = client.post(
            "/api/v1/web/campaigns",
            json={
                "criteria": {
                    "ai_budget_usd": 10,
                    "name": "Network",
                    "include_keywords": ["network"],
                }
            },
        ).json()
        preview = client.post(
            "/api/v1/web/matching/preview",
            json={
                "campaign_id": campaign["id"],
                "tender_ids": [tender["id"]],
                "mode": "semantic",
            },
        ).json()
        assert fixture.calls == []
        assert (
            client.post(f"/api/v1/web/matching/{preview['id']}/confirm", json={}).status_code == 202
        )
        operation = client.get(f"/api/v1/web/operations/{preview['id']}").json()
        results = client.get("/api/v1/web/results", params={"campaign_id": campaign["id"]}).json()[
            "items"
        ]
        assert len(fixture.calls) == 1
        assert len(results) == 1
        outcome = results[0]["outcome"]
        assert outcome["result"] == "unsure"
        assert outcome["model_calls"][0]["attempts"] == 1
        if behavior in {"unavailable", "invalid_response"}:
            assert operation["status"] == "failed"
            assert outcome["model_calls"][0]["error_code"]
        elif behavior == "invalid_evidence":
            assert outcome["model_opinion"]["decision"] == "match"
            assert outcome["evidence_verification"]["status"] == "invalid"
        else:
            assert operation["status"] == "completed"
            assert outcome["model_opinion"]["decision"] == "unsure"
            assert outcome["model_calls"][0]["error_code"] is None


def test_matching_confirmation_rejects_changed_criteria(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        tender = client.post(
            "/api/v1/web/tenders",
            json={
                "request_id": str(uuid4()),
                "title": "Network router",
                "deadline": "2026-12-01T00:00:00Z",
            },
        ).json()
        campaign = client.post("/api/v1/web/campaigns", json={"criteria": {"name": "Old"}}).json()
        preview = client.post(
            "/api/v1/web/matching/preview",
            json={
                "campaign_id": campaign["id"],
                "tender_ids": [tender["id"]],
                "mode": "semantic",
            },
        ).json()
        updated = client.post(
            f"/api/v1/web/campaigns/{campaign['id']}/criteria",
            json={
                "criteria": {"name": "New"},
                "expected_version_id": campaign["current_criteria_version_id"],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["criteria_number"] == 2
        assert (
            client.post(f"/api/v1/web/matching/{preview['id']}/confirm", json={}).status_code == 409
        )
        assert fixture.calls == []
        assert (
            client.post(
                "/api/v1/web/matching/preview",
                json={
                    "campaign_id": campaign["id"],
                    "tender_ids": [tender["id"]] * 501,
                    "mode": "semantic",
                },
            ).status_code
            == 422
        )


def test_manual_tender_replay_and_validation(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        body = {
            "request_id": str(uuid4()),
            "title": "Manual router",
            "value": "100",
            "currency": "USD",
        }
        first = client.post("/api/v1/web/tenders", json=body)
        assert first.status_code == 201
        replay = client.post("/api/v1/web/tenders", json=body)
        assert replay.status_code == 201
        assert replay.json()["id"] == first.json()["id"]
        detail = client.get("/api/v1/web/tenders/" + first.json()["id"]).json()
        assert len(detail["history"]) == 1
        assert detail["metadata"]["provenance"] == "manual"
        assert (
            client.post("/api/v1/web/tenders", json={**body, "title": "Different"}).status_code
            == 409
        )
        assert (
            client.post(
                "/api/v1/web/tenders", json={**body, "request_id": str(uuid4()), "value": "-1"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/v1/web/sources",
                json={"source_id": "manual", "name": "Reserved", "url": fixture.source_url},
            ).status_code
            == 422
        )


def test_automatic_validation_replacement_and_limits(migrated_engine: Engine) -> None:
    from tender_system.persistence.storage import ActiveRecipeRow, RecipeRevisionRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        key = add_source(client, fixture.source_url)
        root = f"/api/v1/web/sources/{key}"
        bounds = {
            "max_requests": 20,
            "max_duration_seconds": 45,
            "max_response_bytes": 2000000,
            "max_total_response_bytes": 8000000,
        }
        assert client.patch(root, json={"enabled": False}).status_code == 200
        assert (
            client.put(
                root + "/configuration",
                json={
                    "configuration": {
                        "timezone": "Asia/Tashkent",
                        "timeout_seconds": 7,
                        "retry": {"max_attempts": 1},
                    }
                },
            ).status_code
            == 200
        )
        setup = save_recipe(client, key, fixture.recipe, **bounds)
        assert setup["status"] == "activated", setup
        source = next(
            s for s in client.get("/api/v1/web/sources").json()["items"] if s["id"] == key
        )
        assert not source["enabled"]  # explicit preference survives activation
        old_version = source["recipe_version"]
        assert client.post(root + "/collect", json={"max_tenders": 2}).status_code == 409
        assert client.patch(root, json={"enabled": True}).status_code == 200
        assert client.post(root + "/collect", json={"max_tenders": 3}).status_code == 202
        assert client.post(root + "/collect", json={"max_tenders": 2}).status_code == 202
        # Execution failure must leave the old active revision intact.
        changed = json.loads(fixture.recipe)
        changed.pop("source_id")
        changed.pop("version")
        changed["steps"][1]["parameters"]["selector"] = "missing"
        failed = save_recipe(client, key, json.dumps(changed), **bounds)
        assert failed["status"] == "failed", failed
        with session_factory(migrated_engine)() as session:
            active = session.get(ActiveRecipeRow, key)
            assert active is not None and active.version == old_version
        # Correcting a failed replacement needs no reject/approval step or manual version bump.
        changed["steps"][1]["parameters"]["selector"] = "lots"
        fixture.alternate_listing = True
        replaced = save_recipe(client, key, json.dumps(changed), **bounds)
        assert replaced["status"] == "activated", replaced
        with session_factory(migrated_engine)() as session:
            active = session.get(ActiveRecipeRow, key)
            assert active is not None and active.version != old_version
            assert session.get(RecipeRevisionRow, (key, old_version)) is not None
        assert client.post(root + "/collect", json={"max_tenders": 2}).status_code == 202
        assert client.get("/api/v1/web/tenders").json()["total"] == 2
        policy = client.get(root + "/configuration").json()["configuration"]
        assert policy["timezone"] == "Asia/Tashkent" and policy["timeout_seconds"] == 7
        invalid = client.post(
            root + "/recipe", json={"text": fixture.recipe, "max_requests": 10001}
        )
        assert invalid.status_code == 422
        bad = client.put(
            root + "/configuration",
            json={
                "configuration": {
                    "normalization": {"decimal_separator": ",", "thousands_separator": ","}
                }
            },
        )
        assert bad.status_code == 422
        assert fixture.calls == []


def test_old_draft_stays_accessible_and_canonical(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from tender_system.persistence.storage import OperationRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        source_id = add_source(client, fixture.source_url)
        tick = fixture.runtime.tick
        monkeypatch.setattr(fixture.runtime, "tick", lambda key=None: False)
        imported = client.post(
            f"/api/v1/web/sources/{source_id}/recipe", json={"text": fixture.recipe}
        ).json()
        now = datetime.now(UTC)
        with session_factory(migrated_engine).begin() as session:
            session.add_all(
                [
                    OperationRow(
                        id=uuid4(),
                        kind="collection",
                        source_id=source_id,
                        status="completed",
                        payload={},
                        result={},
                        created_at=now,
                        started_at=now,
                        finished_at=now,
                    )
                    for _ in range(101)
                ]
            )
        assert imported["id"] not in [r["id"] for r in client.get("/api/v1/web/activity").json()]
        workspace = client.get(f"/api/v1/web/sources/{source_id}/workspace").json()
        assert workspace["recipe_import"]["result"]["manual_recipe_import"] is True
        assert len(workspace["operations"]) == 100
        saved = client.get(f"/api/v1/web/sources/{source_id}/recipe?stage=draft").json()
        recipe = json.loads(saved["text"])
        assert recipe["source_id"] == source_id
        assert recipe["steps"] == json.loads(fixture.recipe)["steps"]
        assert (
            client.post(
                f"/api/v1/web/sources/{source_id}/recipe", json={"text": fixture.recipe}
            ).status_code
            == 409
        )

        monkeypatch.setattr(fixture.runtime, "tick", tick)
        resumed = client.post(f"/api/v1/web/operations/{imported['id']}/resume", json={})
        assert resumed.status_code == 202
        assert (
            client.get("/api/v1/web/operations/" + imported["id"]).json()["status"] == "activated"
        )


def test_resume_and_explicit_new_semantic_evaluation(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        tender = client.post(
            "/api/v1/web/tenders",
            json={
                "request_id": str(uuid4()),
                "title": "Network router",
                "deadline": "2026-12-01T00:00:00Z",
                "product_items": [{"name": "Network router"}],
            },
        ).json()
        campaign = client.post(
            "/api/v1/web/campaigns",
            json={
                "criteria": {
                    "ai_budget_usd": 10,
                    "name": "Network",
                    "include_keywords": ["network"],
                }
            },
        ).json()
        body = {"campaign_id": campaign["id"], "tender_ids": [tender["id"]], "mode": "semantic"}
        preview = client.post("/api/v1/web/matching/preview", json=body).json()
        assert preview["result"]["maximum_output_tokens"] == 600
        assert preview["result"]["input_token_estimate"] is None
        assert (
            client.post(f"/api/v1/web/operations/{preview['id']}/resume", json={}).status_code
            == 409
        )
        tick = fixture.runtime.tick
        monkeypatch.setattr(fixture.runtime, "tick", lambda key=None: False)
        assert (
            client.post(f"/api/v1/web/matching/{preview['id']}/confirm", json={}).status_code == 202
        )
        assert client.get("/api/v1/web/activity").json()[0]["status"] == "queued"
        assert fixture.calls == []
        monkeypatch.setattr(fixture.runtime, "tick", tick)
        fixture.fail_model = True
        assert (
            client.post(f"/api/v1/web/operations/{preview['id']}/resume", json={}).status_code
            == 202
        )
        assert len(fixture.calls) == 1
        assert client.get("/api/v1/web/operations/" + preview["id"]).json()["status"] == "failed"
        assert (
            client.post(f"/api/v1/web/operations/{preview['id']}/resume", json={}).status_code
            == 409
        )
        fixture.fail_model = False
        cached = client.post("/api/v1/web/matching/preview", json=body).json()
        client.post(f"/api/v1/web/matching/{cached['id']}/confirm", json={})
        assert len(fixture.calls) == 1
        fresh = client.post("/api/v1/web/matching/preview", json={**body, "force_new": True}).json()
        assert fresh["result"]["new_evaluation"] is True
        assert len(fixture.calls) == 1
        assert (
            client.post(f"/api/v1/web/matching/{fresh['id']}/confirm", json={}).status_code == 202
        )
        assert len(fixture.calls) == 2
        assert client.get("/api/v1/web/operations/" + fresh["id"]).json()["status"] == "completed"
        results = client.get(
            "/api/v1/web/results", params={"campaign_id": campaign["id"], "limit": 1}
        ).json()
        assert results["total"] == 2 and len(results["items"]) == 1
        second = client.get(
            "/api/v1/web/results", params={"campaign_id": campaign["id"], "limit": 1, "offset": 1}
        ).json()
        assert second["items"][0]["id"] != results["items"][0]["id"]
        failures = client.get(
            "/api/v1/web/results",
            params={"campaign_id": campaign["id"], "state": "technical_error"},
        ).json()
        assert failures["total"] == 1
        matches = client.get(
            "/api/v1/web/results", params={"campaign_id": campaign["id"], "state": "match"}
        ).json()
        assert matches["total"] == 1
        assert (
            client.get(
                "/api/v1/web/results", params={"campaign_id": campaign["id"], "state": "unsure"}
            ).json()["total"]
            == 0
        )


def test_active_campaign_options_are_searchable_and_paginated(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        for i in range(101):
            response = client.post(
                "/api/v1/web/campaigns", json={"criteria": {"name": f"Campaign {i:03}"}}
            )
            assert response.status_code == 201
        first = client.get("/api/v1/web/campaign-options").json()
        assert first["total"] == 101 and len(first["items"]) == 50
        last = client.get("/api/v1/web/campaign-options?offset=100").json()
        assert len(last["items"]) == 1
        found = client.get("/api/v1/web/campaign-options?q=Campaign%20100").json()
        assert found["total"] == 1 and found["items"][0]["name"] == "Campaign 100"
        selected = found["items"][0]
        client.patch("/api/v1/web/campaigns/" + selected["id"] + "/state", json={"active": False})
        assert client.get("/api/v1/web/campaign-options?q=Campaign%20100").json()["total"] == 0
        assert fixture.calls == []


@pytest.mark.parametrize("changed", ["disabled", "budget", "campaign"])
def test_queued_matching_rechecks_server_restrictions(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    from uuid import UUID

    from tender_system.api.api import create_app
    from tender_system.persistence.storage import CampaignRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        tender = client.post(
            "/api/v1/web/tenders",
            json={
                "request_id": str(uuid4()),
                "title": "Network router",
                "deadline": "2026-12-01T00:00:00Z",
                "product_items": [{"name": "Network router"}],
            },
        ).json()
        campaign = client.post(
            "/api/v1/web/campaigns",
            json={
                "criteria": {
                    "ai_budget_usd": 10,
                    "name": "Network",
                    "include_keywords": ["network"],
                },
            },
        ).json()
        preview = client.post(
            "/api/v1/web/matching/preview",
            json={
                "campaign_id": campaign["id"],
                "tender_ids": [tender["id"]],
                "mode": "semantic",
            },
        ).json()
        tick = fixture.runtime.tick
        monkeypatch.setattr(fixture.runtime, "tick", lambda key=None: False)
        assert (
            client.post(f"/api/v1/web/matching/{preview['id']}/confirm", json={}).status_code == 202
        )
        config = fixture.runtime.config
        if changed == "campaign":
            with session_factory(migrated_engine).begin() as session:
                row = session.get(CampaignRow, UUID(campaign["id"]))
                assert row is not None
                row.active = False
        elif changed == "disabled":
            config = config.model_copy(
                update={"runtime": config.runtime.model_copy(update={"ai_semantic_enabled": False})}
            )
        else:
            config = config.model_copy(
                update={
                    "global_settings": config.global_settings.model_copy(
                        update={
                            "campaign": config.global_settings.campaign.model_copy(
                                update={"budget_limit": 0}
                            )
                        }
                    )
                }
            )
        fixture.runtime.config = config
        restarted_app = create_app(
            session_factory(migrated_engine),
            config,
            runtime=fixture.runtime,
        )
        with TestClient(restarted_app) as restarted:
            assert (
                restarted.post(
                    f"/api/v1/web/operations/{preview['id']}/resume", json={}
                ).status_code
                == 409
            )
        monkeypatch.setattr(fixture.runtime, "tick", tick)
        assert fixture.runtime.tick(UUID(preview["id"]))
        status = client.get("/api/v1/web/operations/" + preview["id"]).json()
        assert status["status"] == "failed", status
        assert fixture.calls == []


@pytest.mark.parametrize(
    "field,tightened",
    [
        ("max_requests", 1),
        ("max_duration_seconds", 1),
        ("max_response_bytes", 1000),
        ("max_total_response_bytes", 1000),
    ],
)
def test_queued_collection_uses_run_budgets_instead_of_validation_budgets(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, field: str, tightened: int
) -> None:

    from tender_system.persistence.storage import OperatorConfigRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        root = "/api/v1/web"
        source_id = add_source(client, fixture.source_url)
        setup = save_recipe(client, source_id, fixture.recipe)
        assert setup["status"] == "activated", setup
        tick = fixture.runtime.tick
        monkeypatch.setattr(fixture.runtime, "tick", lambda key=None: False)
        operation = client.post(
            root + f"/sources/{source_id}/collect", json={"max_tenders": 2}
        ).json()
        with session_factory(migrated_engine).begin() as session:
            row = session.get(OperatorConfigRow, f"source/{source_id}")
            assert row is not None
            scope = row.value["approved_execution_limits"]
            assert isinstance(scope, dict)
            row.value = {**row.value, "approved_execution_limits": {**scope, field: tightened}}
        from tender_system.api.web import Bounds
        from tender_system.collection.recipes import RecipeExecutor
        from tender_system.core.domain import (
            CollectionRun,
            ExecutionContext,
            SourceFailure,
            SourceRecipe,
        )
        from tender_system.core.execution import current_resource_limits

        requested = getattr(Bounds(), field)
        observed: list[int | float] = []
        execute = RecipeExecutor.execute

        def bounded_execute(
            self: RecipeExecutor, recipe: SourceRecipe, context: ExecutionContext
        ) -> CollectionRun | SourceFailure:
            assert context.limits is not None
            observed.append(getattr(context.limits, field))
            assert getattr(current_resource_limits(), field) == requested
            return execute(self, recipe, context)

        monkeypatch.setattr(RecipeExecutor, "execute", bounded_execute)
        monkeypatch.setattr(fixture.runtime, "tick", tick)
        assert (
            client.post(root + f"/operations/{operation['id']}/resume", json={}).status_code == 202
        )
        status = client.get("/api/v1/web/operations/" + operation["id"]).json()
        assert status["status"] in {"completed", "failed"}, status
        assert observed == [requested]


@pytest.mark.parametrize("guard", ["witness", "replay", "fresh_check"])
def test_source_setup_retains_validation_guards(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, guard: str
) -> None:
    from tender_system.collection.validation_evidence import CapturedResponseHttpAdapter
    from tender_system.core.clock import SystemClock
    from tender_system.core.domain import SampleVerification
    from tender_system.operator.operations import OperationRestriction
    from tender_system.operator.source_approval import verified
    from tender_system.persistence.storage import AccountSourceRow, ActiveRecipeRow, UserRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        key = add_source(client, fixture.source_url)
        with monkeypatch.context() as patch:
            if guard == "witness":
                patch.setattr(
                    fixture.runtime.sample_verifier,
                    "verify",
                    lambda *args: SampleVerification(checked=0),
                )
            elif guard == "replay":

                def mismatch(*args: object) -> None:
                    raise ValueError("Replay capture mismatch")

                patch.setattr(CapturedResponseHttpAdapter, "assert_exhausted", mismatch)
            else:

                def stale(*args: object) -> None:
                    raise OperationRestriction("Source changed during fresh checks")

                patch.setattr(fixture.runtime, "fresh_source_check", stale)
            failed = save_recipe(client, key, fixture.recipe)
            assert failed["status"] == "failed", failed
            with session_factory(migrated_engine)() as session:
                assert session.get(ActiveRecipeRow, key) is None
        corrected = save_recipe(client, key, fixture.recipe)
        assert corrected["status"] == "activated", corrected
        # Customer-source eligibility must recognize the real validation and
        # activation records, not merely an active recipe row.
        with session_factory(migrated_engine).begin() as session:
            user_id = uuid4()
            session.add(
                UserRow(
                    id=user_id,
                    email=f"{user_id}@example.test",
                    password_hash="test",
                    role="user",
                    active=True,
                    created_at=SystemClock().now(),
                )
            )
            session.flush()
            session.add(AccountSourceRow(source_id=key, user_id=user_id))
            session.flush()
            assert verified(session, key)
        assert fixture.calls == []


def test_source_setup_reconciles_committed_activation(migrated_engine: Engine) -> None:
    from datetime import UTC, datetime
    from uuid import UUID

    from tender_system.operator.operations import recover_operations
    from tender_system.persistence.storage import OperationRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        key = add_source(client, fixture.source_url)
        setup = save_recipe(client, key, fixture.recipe)
        assert setup["status"] == "activated", setup
        with session_factory(migrated_engine).begin() as session:
            row = session.get(OperationRow, UUID(str(setup["id"])))
            assert row is not None
            # Simulate disconnect between activation commit and operation completion.
            row.status, row.result, row.finished_at = "running", {}, None
        with session_factory(migrated_engine).begin() as session:
            assert recover_operations(session, datetime.now(UTC)) == 1
        recovered = client.get("/api/v1/web/operations/" + str(setup["id"])).json()
        assert recovered["status"] == "activated"
        assert recovered["result"]["source_activation_persisted"] is True


def test_tender_listing_uses_bounded_queries(migrated_engine: Engine) -> None:
    from sqlalchemy import event

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        for index in range(6):
            response = client.post(
                "/api/v1/web/tenders",
                json={"request_id": str(uuid4()), "title": f"Router {index}"},
            )
            assert response.status_code == 201
        queries: list[str] = []

        def record(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        event.listen(migrated_engine, "before_cursor_execute", record)
        try:
            response = client.get("/api/v1/web/tenders")
        finally:
            event.remove(migrated_engine, "before_cursor_execute", record)
        assert response.status_code == 200
        assert response.json()["total"] == 6
        assert len(response.json()["items"]) == 6
        assert len(queries) <= 4, queries


def test_collection_limit_changes_without_recipe_validation(migrated_engine: Engine) -> None:
    from sqlalchemy import func, select

    from tender_system.persistence.storage import ActiveRecipeRow, RecipeRevisionRow

    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        fixture.record_count = 12
        key = add_source(client, fixture.source_url)
        response = client.post(f"/api/v1/web/sources/{key}/recipe", json={"text": fixture.recipe})
        assert response.status_code == 202, response.text
        setup = client.get("/api/v1/web/operations/" + response.json()["id"]).json()
        assert setup["status"] == "activated", setup
        result = setup["result"]
        assert isinstance(result, dict)
        assert result["recipe_status"] == "valid"
        assert result["verification"]["checked"] == 3
        assert result["verification"]["issues"] == []
        assert set(result["timings_seconds"]) == {
            "extraction",
            "normalization_and_health",
            "independent_verification",
            "offline_replay",
            "activation",
        }
        assert result["duration_seconds"] >= result["validation_duration_seconds"]
        root = f"/api/v1/web/sources/{key}"
        for count in (10, 11, 11):
            response = client.post(root + "/collect", json={"max_tenders": count})
            assert response.status_code == 202, response.text
            operation = client.get("/api/v1/web/operations/" + response.json()["id"]).json()
            assert operation["status"] == "completed", operation
            assert operation["result"]["records_processed"] == count
            assert operation["result"]["collection_status"] == "bounded_success"
            assert operation["result"]["coverage_status"] == "incomplete"
            assert operation["result"]["health"]["absence_evidence_allowed"] is False
        with session_factory(migrated_engine)() as session:
            assert session.get(ActiveRecipeRow, key) is not None
            assert session.scalar(select(func.count()).select_from(RecipeRevisionRow)) == 1
        assert len([path for path in fixture.requests if path.startswith("/detail/")]) == 3
        catalog = client.get(
            root.replace("/sources/" + key, "/tenders"), params={"source": key}
        ).json()
        assert catalog["total"] == 11
        for tender in catalog["items"]:
            detail = client.get("/api/v1/web/tenders/" + tender["id"]).json()
            assert len(detail["history"]) == 1
        assert fixture.calls == []


def test_scraping_configuration_errors_identify_fields(migrated_engine: Engine) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        key = add_source(client, fixture.source_url)
        root = f"/api/v1/web/sources/{key}"
        response = client.post(
            root + "/collect",
            json={
                "max_response_bytes": 10000,
                "max_total_response_bytes": 1000,
            },
        )
        assert response.status_code == 422
        fields = response.json()["error"]["fields"]
        assert fields == [
            {
                "field": "max_total_response_bytes",
                "code": "value_error",
                "message": (
                    "Value error, Total download limit must be at least the per-response limit"
                ),
            }
        ]
        response = client.put(
            root + "/configuration",
            json={
                "configuration": {
                    "timeout_seconds": 0,
                }
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["fields"][0]["field"] == "configuration.timeout_seconds"
        assert "input" not in response.text
