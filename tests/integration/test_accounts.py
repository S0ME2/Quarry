"""Real PostgreSQL account workflows and adversarial ownership boundaries."""

from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from tender_system.api.api import create_app
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import Configuration
from tender_system.persistence.storage import (
    AccountCampaignRow,
    AccountSourceRow,
    ActiveRecipeRow,
    OperationRow,
    OperatorConfigRow,
    RecipeRevisionRow,
    SourceRow,
    UserRow,
    UserSessionRow,
    account_campaign_allowed,
    session_factory,
)
from tender_system.testing.web import web_fixture

pytestmark = pytest.mark.database
PASSWORD = "test-password-long-enough"


def register(client: TestClient, email: str) -> dict[str, object]:
    response = client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return dict(response.json())


def verified_source(session: object, key: str) -> None:
    from sqlalchemy.orm import Session

    from tender_system.collection.validation_evidence import fingerprint

    assert isinstance(session, Session)
    session.flush()
    session.add(
        RecipeRevisionRow(
            source_id=key,
            version="1",
            revision={"recipe": {"version": "1"}},
            created_at=SystemClock().now(),
        )
    )
    session.flush()
    session.add(ActiveRecipeRow(source_id=key, version="1"))
    session.add(
        OperationRow(
            id=uuid4(),
            kind="recipe_generation",
            source_id=key,
            status="activated",
            payload={},
            result={
                "validated_recipe_fingerprint": fingerprint({"version": "1"}),
                "validation_evidence_reference": "test-evidence",
                "activation_operation_id": str(uuid4()),
            },
            created_at=SystemClock().now(),
        )
    )
    session.add(
        OperatorConfigRow(
            key="web/source/" + key,
            value={"source_id": key, "name": key, "url": f"https://{key}.example/"},
            updated_at=SystemClock().now(),
        )
    )


def test_accounts_registration_sessions_and_login_lockout(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    app = create_app(sessions, operator_mode=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/web/sources").status_code == 401
        assert client.get("/api/v1/auth/me").status_code == 401
        assert (
            client.post(
                "/api/v1/auth/register", json={"email": "invalid", "password": PASSWORD}
            ).status_code
            == 422
        )
        user = register(client, "First@Example.com")
        assert user["role"] == "user"
        assert user["email"] == "first@example.com"
        assert (
            "httponly"
            in client.post(
                "/api/v1/auth/login", json={"email": "first@example.com", "password": PASSWORD}
            )
            .headers["set-cookie"]
            .lower()
        )
        assert client.get("/api/v1/web/sources").status_code == 403
        assert client.get("/api/v1/admin/users").status_code == 403
        assert client.get("/api/v1/account/campaigns").json() == {"items": []}
        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401
        for _ in range(5):
            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"email": "first@example.com", "password": "incorrect-password"},
                ).status_code
                == 401
            )
        with sessions() as session:
            row = session.get(UserRow, UUID(str(user["id"])))
            assert row is not None
            locked_until = row.locked_until
            assert locked_until is not None
            assert row.password_hash != PASSWORD
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "first@example.com", "password": PASSWORD}
            ).status_code
            == 401
        )
        with sessions() as session:
            row = session.get(UserRow, UUID(str(user["id"])))
            assert row is not None
            assert row.locked_until == locked_until


def test_expired_revoked_and_disabled_sessions(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    app = create_app(sessions, operator_mode=False)
    with TestClient(app) as client:
        user = register(client, "expiry@example.com")
        with sessions.begin() as session:
            row = session.scalar(select(UserSessionRow))
            assert row is not None
            row.expires_at = SystemClock().now() - timedelta(seconds=1)
        assert client.get("/api/v1/account/sources").status_code == 401
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "expiry@example.com", "password": PASSWORD}
            ).status_code
            == 200
        )
        with sessions.begin() as session:
            account = session.get(UserRow, UUID(str(user["id"])))
            assert account is not None
            account.active = False
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.get("/api/v1/account/sources").status_code == 401


def test_same_origin_and_auth_throttle(migrated_engine: Engine) -> None:
    app = create_app(session_factory(migrated_engine), operator_mode=False)
    with TestClient(app) as client:
        for path in ("/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/logout"):
            assert (
                client.post(
                    path,
                    headers={"Origin": "https://evil.example"},
                    json={"email": "foo@example.com", "password": PASSWORD},
                ).status_code
                == 403
            )
        for _ in range(30):
            assert client.post("/api/v1/auth/login", json={}).status_code == 422
        response = client.post("/api/v1/auth/login", json={})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"


def test_campaign_ownership_results_and_admin_permissions(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    with web_fixture(migrated_engine) as fixture:
        secure = create_app(
            sessions, fixture.runtime.config, runtime=fixture.runtime, operator_mode=False
        )
        with (
            TestClient(secure) as alice,
            TestClient(secure) as bob,
            TestClient(fixture.app) as operator,
        ):
            first = register(alice, "alice@example.com")
            second = register(bob, "bob@example.com")
            with sessions.begin() as session:
                session.add(SourceRow(id="manual", name="Manual", has_stable_external_id=True))
                verified_source(session, "manual")
            body = {
                "criteria": {
                    "name": "Network",
                    "source_ids": ["manual"],
                    "include_keywords": ["network"],
                    "notifications": {
                        "enabled": True,
                        "channel": "email",
                        "email": "wrong@example.com",
                    },
                },
                "interval_seconds": 3600,
            }
            response = alice.post("/api/v1/account/campaigns", json=body)
            assert response.status_code == 201, response.text
            campaign = response.json()
            assert campaign["criteria"]["notifications"]["email"] == "alice@example.com"
            assert campaign["criteria"]["automatic_mode"] == "rules"
            key = campaign["id"]
            assert bob.get("/api/v1/account/campaigns").json() == {"items": []}
            assert bob.put(f"/api/v1/account/campaigns/{key}", json=body).status_code == 404
            assert bob.delete(f"/api/v1/account/campaigns/{key}").status_code == 404
            assert alice.put(f"/api/v1/account/campaigns/{key}", json=body).status_code == 409
            updated = alice.put(
                f"/api/v1/account/campaigns/{key}",
                json={
                    **body,
                    "expected_version_id": campaign["current_criteria_version_id"],
                    "interval_seconds": 10800,
                },
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["interval_seconds"] == 10800
            assert (
                updated.json()["current_criteria_version_id"]
                == campaign["current_criteria_version_id"]
            )
            tender = operator.post(
                "/api/v1/web/tenders",
                json={
                    "request_id": str(uuid4()),
                    "title": "Network equipment",
                    "product_items": [{"name": "Network router"}],
                },
            ).json()
            preview = operator.post(
                "/api/v1/web/matching/preview",
                json={"campaign_id": key, "tender_ids": [tender["id"]], "mode": "rules"},
            )
            assert preview.status_code == 201, preview.text
            confirmed = operator.post(
                f"/api/v1/web/matching/{preview.json()['id']}/confirm", json={}
            )
            assert confirmed.status_code == 202, confirmed.text
            results = alice.get("/api/v1/account/tenders").json()
            assert results["total"] == 1, results
            for relevance in ("RELEVANT", "NOT_RELEVANT", "NEEDS_REVIEW"):
                response = alice.get("/api/v1/account/tenders", params={"relevance": relevance})
                assert response.status_code == 200, response.text
                assert all(item["relevance"] == relevance for item in response.json()["items"])
            detail = alice.get(f"/api/v1/account/tenders/{tender['id']}")
            assert detail.status_code == 200, detail.text
            assert "analysis" in detail.json()
            current = updated.json()
            for enabled in (False, True):
                changed = alice.put(
                    f"/api/v1/account/campaigns/{key}",
                    json={
                        "criteria": current["criteria"],
                        "active": enabled,
                        "expected_version_id": current["current_criteria_version_id"],
                    },
                )
                assert changed.status_code == 200, changed.text
                assert (
                    changed.json()["current_criteria_version_id"]
                    == current["current_criteria_version_id"]
                )
                assert alice.get("/api/v1/account/tenders").json()["total"] == 1
                assert alice.get(f"/api/v1/account/tenders/{tender['id']}").status_code == 200
            notification_update = alice.put(
                f"/api/v1/account/campaigns/{key}",
                json={
                    "criteria": {**current["criteria"], "notifications": {"enabled": False}},
                    "expected_version_id": current["current_criteria_version_id"],
                },
            )
            assert notification_update.status_code == 200, notification_update.text
            current = notification_update.json()
            retained = alice.get("/api/v1/account/tenders").json()
            assert retained["total"] == 1
            assert not retained["items"][0]["analysis_stale"]
            with sessions.begin() as session:
                session.add(
                    OperatorConfigRow(
                        key="source-review/manual",
                        value={"decision": "rejected"},
                        updated_at=SystemClock().now(),
                    )
                )
            paused = alice.put(
                f"/api/v1/account/campaigns/{key}",
                json={
                    "criteria": current["criteria"],
                    "active": False,
                    "expected_version_id": current["current_criteria_version_id"],
                },
            )
            assert paused.status_code == 200, paused.text
            assert bob.get("/api/v1/account/tenders").json()["total"] == 0
            assert bob.get(f"/api/v1/account/tenders/{tender['id']}").status_code == 404
            assert (
                alice.get(
                    "/api/v1/account/tenders", params={"published_from": "2026-01-01"}
                ).status_code
                == 200
            )
            with sessions.begin() as session:
                row = session.get(UserRow, UUID(str(first["id"])))
                assert row is not None
                row.role = "admin"
            assert (
                alice.get("/api/v1/admin/users", params={"q": "bob"}).json()["items"][0]["id"]
                == second["id"]
            )
            assert (
                alice.patch(
                    f"/api/v1/admin/users/{first['id']}", json={"active": False}
                ).status_code
                == 409
            )
            assert (
                alice.patch(
                    f"/api/v1/admin/users/{second['id']}", json={"active": False}
                ).status_code
                == 200
            )
            assert bob.get("/api/v1/account/campaigns").status_code == 401
            assert alice.get(f"/api/v1/admin/users/{first['id']}/campaigns").json()["items"]
            with sessions.begin() as session:
                active = session.get(ActiveRecipeRow, "manual")
                assert active is not None
                session.delete(active)
            assert alice.get("/api/v1/web/sources").status_code == 200


def test_private_source_visibility_and_matching_fence(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    app = create_app(sessions, operator_mode=False)
    with TestClient(app) as alice, TestClient(app) as bob:
        first = register(alice, "private@example.com")
        register(bob, "other@example.com")
        with sessions.begin() as session:
            session.add(SourceRow(id="secret", name="Secret source", has_stable_external_id=True))
            session.add(AccountSourceRow(source_id="secret", user_id=UUID(str(first["id"]))))
        assert alice.get("/api/v1/account/sources").json()["items"][0]["id"] == "secret"
        assert bob.get("/api/v1/account/sources").json() == {"items": []}
        body = {
            "criteria": {
                "name": "Private",
                "source_ids": ["secret"],
                "notifications": {"enabled": False},
            }
        }
        assert bob.post("/api/v1/account/campaigns", json=body).status_code == 404
        assert alice.post("/api/v1/account/campaigns", json=body).status_code == 404
        with sessions.begin() as session:
            verified_source(session, "secret")
            session.add(
                OperatorConfigRow(
                    key="source-review/secret",
                    value={"decision": "approved"},
                    updated_at=SystemClock().now(),
                )
            )
        assert alice.post("/api/v1/account/campaigns", json=body).status_code == 201
        assert bob.post("/api/v1/account/campaigns", json=body).status_code == 201
        with sessions() as session:
            owner = session.scalar(select(AccountCampaignRow))
            assert owner is not None
            assert account_campaign_allowed(session, owner.campaign_id, "secret")
            assert not account_campaign_allowed(session, uuid4(), "secret")


def test_account_scheduler_interval_and_disabled_user(migrated_engine: Engine) -> None:
    from tender_system.operator.operator_runtime import OperatorRuntime
    from tender_system.testing.fakes import FakeClock

    sessions = session_factory(migrated_engine)
    now = SystemClock().now()
    app = create_app(sessions, operator_mode=False)
    with TestClient(app) as client:
        user = register(client, "schedule@example.com")
        with sessions.begin() as session:
            session.add(SourceRow(id="scheduled", name="Scheduled", has_stable_external_id=True))
            verified_source(session, "scheduled")
            session.add(AccountSourceRow(source_id="scheduled", user_id=UUID(str(user["id"]))))
            session.add(
                OperatorConfigRow(
                    key="source-review/scheduled", value={"decision": "approved"}, updated_at=now
                )
            )
        response = client.post(
            "/api/v1/account/campaigns",
            json={
                "criteria": {
                    "name": "Schedule",
                    "source_ids": ["scheduled"],
                    "notifications": {"enabled": False},
                },
                "interval_seconds": 10800,
            },
        )
        assert response.status_code == 201, response.text
    clock = FakeClock(now)
    runtime = OperatorRuntime(sessions, Configuration(), clock=clock)
    try:
        assert runtime.schedule_due() == 1
        with sessions.begin() as session:
            operation = session.scalar(
                select(OperationRow).where(OperationRow.kind == "collection")
            )
            assert operation is not None
            operation.status = "completed"
        clock.instant += timedelta(seconds=3600)
        assert runtime.schedule_due() == 0
        clock.instant += timedelta(seconds=7200)
        assert runtime.schedule_due() == 1
        with sessions.begin() as session:
            for row in session.scalars(
                select(OperationRow).where(OperationRow.kind == "collection")
            ):
                row.status = "completed"
            account = session.get(UserRow, UUID(str(user["id"])))
            assert account is not None
            account.active = False
        clock.instant += timedelta(seconds=10800)
        assert runtime.schedule_due() == 0
    finally:
        runtime.close()


def test_catalog_source_requires_independent_verification(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    with sessions.begin() as session:
        session.add(SourceRow(id="catalog", name="Catalog", has_stable_external_id=True))
        session.flush()
        session.add(
            RecipeRevisionRow(
                source_id="catalog", version="1", revision={}, created_at=SystemClock().now()
            )
        )
        session.flush()
        session.add(ActiveRecipeRow(source_id="catalog", version="1"))
    with TestClient(create_app(sessions, operator_mode=False)) as client:
        register(client, "catalog-unverified@example.com")
        assert client.get("/api/v1/account/sources").json() == {"items": []}
        response = client.post(
            "/api/v1/account/campaigns",
            json={
                "criteria": {
                    "name": "Catalog",
                    "source_ids": ["catalog"],
                    "notifications": {"enabled": False},
                }
            },
        )
        assert response.status_code == 404


def test_legacy_source_name_requires_review_without_merging(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    with sessions.begin() as session:
        session.add(SourceRow(id="legacy", name="Example tenders", has_stable_external_id=True))
    with (
        TestClient(create_app(sessions, operator_mode=False)) as client,
        TestClient(create_app(sessions, operator_mode=False)) as admin,
    ):
        register(client, "legacy-review@example.com")
        administrator = register(admin, "legacy-admin@example.com")
        with sessions.begin() as session:
            account = session.get(UserRow, UUID(str(administrator["id"])))
            assert account is not None
            account.role = "admin"
        response = client.post(
            "/api/v1/account/sources",
            json={"name": " example TENDERS ", "url": "https://example.org/"},
        )
        assert response.status_code == 201
        key = response.json()["id"]
        assert (
            client.post(
                "/api/v1/account/sources", json={"name": "Example", "url": "https://example.org"}
            ).json()["id"]
            == key
        )
        assert client.post(f"/api/v1/account/sources/{key}/check").status_code == 409
        requests = admin.get("/api/v1/admin/source-requests").json()["items"]
        assert requests[0]["identity_candidates"] == [{"id": "legacy", "name": "Example tenders"}]
        assert (
            admin.patch(
                f"/api/v1/admin/source-requests/{key}", json={"decision": "approved"}
            ).status_code
            == 409
        )
        assert (
            admin.post(
                f"/api/v1/admin/source-requests/{key}/resolve",
                json={"existing_source_id": "legacy"},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/account/sources").json()["items"][0]["id"] == "legacy"
        assert (
            client.post(
                "/api/v1/account/sources", json={"name": "Example", "url": "https://example.org"}
            ).json()["id"]
            == "legacy"
        )
        with sessions() as session:
            assert list(session.scalars(select(SourceRow.id))) == ["legacy"]


def test_legacy_similar_name_can_be_distinct(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    with sessions.begin() as session:
        session.add(SourceRow(id="legacy", name="Tenders", has_stable_external_id=True))
    app = create_app(sessions, operator_mode=False)
    with TestClient(app) as client, TestClient(app) as admin:
        register(client, "distinct@example.com")
        administrator = register(admin, "distinct-admin@example.com")
        with sessions.begin() as session:
            account = session.get(UserRow, UUID(str(administrator["id"])))
            assert account is not None
            account.role = "admin"
        key = client.post(
            "/api/v1/account/sources", json={"name": "Tenders", "url": "https://different.example/"}
        ).json()["id"]
        assert (
            admin.post(
                f"/api/v1/admin/source-requests/{key}/resolve", json={"existing_source_id": None}
            ).status_code
            == 200
        )
        assert (
            admin.patch(
                f"/api/v1/admin/source-requests/{key}", json={"decision": "approved"}
            ).status_code
            == 200
        )
        with sessions() as session:
            assert session.get(OperatorConfigRow, "web/source/legacy") is None


def test_source_review_and_shared_catalog(migrated_engine: Engine) -> None:
    from tender_system.collection.validation_evidence import fingerprint
    from tender_system.operator.operations import OperatorService
    from tender_system.operator.operator_runtime import OperatorRuntime

    sessions = session_factory(migrated_engine)
    app = create_app(sessions, operator_mode=False)
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as admin:
        register(alice, "requester@example.com")
        register(bob, "another@example.com")
        administrator = register(admin, "reviewer@example.com")
        with sessions.begin() as session:
            account = session.get(UserRow, UUID(str(administrator["id"])))
            assert account is not None
            account.role = "admin"
        body = {"name": "Tenders", "url": "https://tenders.example.org/"}
        first = alice.post("/api/v1/account/sources", json=body)
        assert first.status_code == 201, first.text
        key = first.json()["id"]
        assert first.json()["status"] == "UNDER_REVIEW"
        duplicate = bob.post(
            "/api/v1/account/sources", json={**body, "url": "https://TENDERS.example.org"}
        )
        assert duplicate.status_code == 201 and duplicate.json()["id"] == key
        assert bob.get("/api/v1/admin/source-requests").status_code == 403
        requests = admin.get("/api/v1/admin/source-requests").json()["items"]
        assert len(requests) == 1 and requests[0]["decision"] == "pending"
        campaign = {
            "criteria": {"name": "One", "source_ids": [key], "notifications": {"enabled": False}}
        }
        assert alice.post("/api/v1/account/campaigns", json=campaign).status_code == 404
        with sessions.begin() as session:
            session.add(SourceRow(id=key, name="Tenders", has_stable_external_id=True))
            session.flush()
            session.add(
                RecipeRevisionRow(
                    source_id=key,
                    version="1",
                    revision={"recipe": {"version": "1"}},
                    created_at=SystemClock().now(),
                )
            )
            session.flush()
            session.add(ActiveRecipeRow(source_id=key, version="1"))
        assert alice.post("/api/v1/account/campaigns", json=campaign).status_code == 404
        with sessions.begin() as session:
            active = session.get(ActiveRecipeRow, key)
            assert active is not None
            session.delete(active)
        review = admin.patch(f"/api/v1/admin/source-requests/{key}", json={"decision": "approved"})
        assert review.status_code == 200 and review.json()["technical_verified"] is False
        assert (
            alice.get("/api/v1/account/sources").json()["items"][0]["status"] == "NEEDS_ATTENTION"
        )
        assert alice.post("/api/v1/account/campaigns", json=campaign).status_code == 404
        with sessions.begin() as session:
            session.add(ActiveRecipeRow(source_id=key, version="1"))
        assert alice.post("/api/v1/account/campaigns", json=campaign).status_code == 404
        assert (
            admin.get("/api/v1/admin/source-requests").json()["items"][0]["technical_verified"]
            is False
        )
        with sessions.begin() as session:
            session.add(
                OperationRow(
                    id=uuid4(),
                    kind="recipe_generation",
                    source_id=key,
                    status="activated",
                    payload={},
                    result={
                        "validated_recipe_fingerprint": fingerprint({"version": "1"}),
                        "validation_evidence_reference": "test-evidence",
                        "activation_operation_id": str(uuid4()),
                    },
                    created_at=SystemClock().now(),
                )
            )
        assert alice.post("/api/v1/account/campaigns", json=campaign).status_code == 201
        assert bob.post("/api/v1/account/campaigns", json=campaign).status_code == 201
        assert bob.get("/api/v1/account/sources").json()["items"][0]["status"] == "APPROVED"
        runtime = OperatorRuntime(sessions, Configuration())
        try:
            with sessions.begin() as session:
                op = OperatorService(session, Configuration(), SystemClock().now()).enqueue(
                    "collection", key, {"account_campaign_ids": ["existing-campaign"]}
                )
                op_id = op.id
            assert (
                admin.patch(
                    f"/api/v1/admin/source-requests/{key}", json={"decision": "rejected"}
                ).status_code
                == 200
            )
            assert alice.get("/api/v1/account/sources").json()["items"][0]["status"] == "REJECTED"
            assert alice.post("/api/v1/account/campaigns", json=campaign).status_code == 404
            assert runtime.schedule_due() == 0
            assert runtime.tick(op_id)
            with sessions() as session:
                rejected = session.get(OperationRow, op_id)
                assert rejected is not None and rejected.status == "failed"
                assert rejected.result["message"] == "source is not approved for monitoring"
        finally:
            runtime.close()


def test_shared_source_account_schedules_merge_and_keep_campaign_intervals(
    migrated_engine: Engine,
) -> None:
    from tender_system.operator.operator_runtime import OperatorRuntime
    from tender_system.testing.fakes import FakeClock

    sessions = session_factory(migrated_engine)
    now = SystemClock().now()
    app = create_app(sessions, operator_mode=False)
    with sessions.begin() as session:
        session.add(SourceRow(id="shared", name="Shared", has_stable_external_id=True))
        verified_source(session, "shared")
    keys = []
    for number, interval in enumerate((3600, 10800)):
        with TestClient(app) as client:
            register(client, f"shared-{number}@example.com")
            response = client.post(
                "/api/v1/account/campaigns",
                json={
                    "criteria": {
                        "name": "Shared",
                        "source_ids": ["shared"],
                        "notifications": {"enabled": False},
                    },
                    "interval_seconds": interval,
                },
            )
            assert response.status_code == 201, response.text
            keys.append(response.json()["id"])
    clock = FakeClock(now)
    runtime = OperatorRuntime(sessions, Configuration(), clock=clock)
    try:
        assert runtime.schedule_due() == 1
        with sessions.begin() as session:
            operation = session.scalar(
                select(OperationRow).where(OperationRow.kind == "collection")
            )
            assert operation is not None
            assert set(cast(list[str], operation.payload["account_campaign_ids"])) == set(keys)
            operation.status = "completed"
        clock.instant += timedelta(seconds=3600)
        assert runtime.schedule_due() == 1
        with sessions() as session:
            operation = session.scalar(select(OperationRow).where(OperationRow.status == "queued"))
            assert operation is not None
            assert operation.payload["account_campaign_ids"] == keys[:1]
    finally:
        runtime.close()


def test_account_scheduled_collection_matching_and_results_end_to_end(
    migrated_engine: Engine,
) -> None:
    from tender_system.persistence.storage import OperatorConfigRow

    sessions = session_factory(migrated_engine)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as operator:
        reply = operator.post("/api/v1/web/sources", json={"url": fixture.source_url})
        assert reply.status_code == 201, reply.text
        key = reply.json()["source_id"]
        imported = operator.post(
            f"/api/v1/web/sources/{key}/recipe", json={"text": fixture.recipe, "max_tenders": 2}
        )
        assert imported.status_code == 202, imported.text
        app = create_app(
            sessions, fixture.runtime.config, runtime=fixture.runtime, operator_mode=False
        )
        with TestClient(app) as client:
            register(client, "journey@example.com")
            assert client.get("/api/v1/account/sources").json()["items"] == []
            enabled = operator.patch(
                f"/api/v1/web/sources/{key}", json={"enabled": True, "manual_only": False}
            )
            assert enabled.status_code == 200, enabled.text
            assert client.get("/api/v1/account/sources").json()["items"][0]["status"] == "APPROVED"
            campaign = client.post(
                "/api/v1/account/campaigns",
                json={
                    "criteria": {
                        "name": "Scheduled network",
                        "source_ids": [key],
                        "include_keywords": ["network"],
                        "notifications": {"enabled": False},
                    },
                    "interval_seconds": 3600,
                },
            )
            assert campaign.status_code == 201, campaign.text
            fixture.runtime.config = fixture.runtime.config.model_copy(
                update={
                    "runtime": fixture.runtime.config.runtime.model_copy(
                        update={"manual_only": False}
                    )
                }
            )
            fixture.record_count = 5
            assert fixture.runtime.schedule_due() == 1
            for _ in range(5):
                if not fixture.runtime.tick():
                    break
            results = client.get("/api/v1/account/tenders").json()
            assert results["total"] == 2, results
            assert all(r["campaign_id"] == campaign.json()["id"] for r in results["items"])
            assert (
                client.get(f"/api/v1/account/tenders/{results['items'][0]['id']}").status_code
                == 200
            )
            coverage = client.get("/api/v1/account/campaigns").json()["items"][0]["monitoring"]
            assert coverage["coverage_status"] == "incomplete", coverage
            assert coverage["status"] == "partial", coverage
            assert coverage["sources"][0]["max_tenders"] == 2
            assert fixture.runtime.schedule_due() == 0
            with sessions.begin() as session:
                scheduled_owner = session.get(AccountCampaignRow, UUID(campaign.json()["id"]))
                assert scheduled_owner is not None and scheduled_owner.last_scheduled_at is not None
                scheduled_owner.last_scheduled_at -= timedelta(hours=2)
            assert fixture.runtime.schedule_due() == 1
            for _ in range(5):
                if not fixture.runtime.tick():
                    break
            repeated = client.get("/api/v1/account/campaigns").json()["items"][0]["monitoring"]
            assert repeated["coverage_status"] == "incomplete", repeated
            assert repeated["status"] == "partial", repeated
            assert client.get("/api/v1/account/tenders").json()["total"] == 2
            current = campaign.json()
            updated = client.put(
                f"/api/v1/account/campaigns/{current['id']}",
                json={
                    "criteria": {
                        **current["criteria"],
                        "automatic_mode": "semantic",
                        "automatic_ai_authorized": True,
                        "ai_request_limit": 2,
                        "ai_budget_usd": 0,
                    },
                    "expected_version_id": current["current_criteria_version_id"],
                    "interval_seconds": 3600,
                },
            )
            assert updated.status_code == 200, updated.text
            with sessions.begin() as session:
                owner = session.get(AccountCampaignRow, UUID(current["id"]))
                assert owner is not None and owner.last_scheduled_at is not None
                owner.last_scheduled_at -= timedelta(hours=1)
            assert fixture.runtime.schedule_due() == 1
            for _ in range(5):
                if not fixture.runtime.tick():
                    break
            monitoring = client.get("/api/v1/account/campaigns").json()["items"][0]["monitoring"]
            assert monitoring["status"] == "blocked", monitoring
            assert monitoring["last_error"]
            retained = client.get("/api/v1/account/tenders").json()
            assert retained["total"] == 2, retained
            assert all(row["analysis_stale"] for row in retained["items"])
            assert not fixture.calls
            with sessions() as session:
                control = session.get(OperatorConfigRow, "source/" + key)
                assert control is not None and not control.value["manual_only"]


def test_queued_filtered_collection_is_unrestricted_when_account_joins(
    migrated_engine: Engine,
) -> None:
    from tender_system.operator.operator_runtime import OperatorRuntime
    from tender_system.testing.fakes import FakeClock

    sessions = session_factory(migrated_engine)
    now = SystemClock().now()
    with sessions.begin() as session:
        session.add(SourceRow(id="shared", name="Shared", has_stable_external_id=True))
        verified_source(session, "shared")
    with TestClient(create_app(sessions, operator_mode=False)) as client:
        register(client, "schedule@example.org")
        campaign = client.post(
            "/api/v1/account/campaigns",
            json={
                "criteria": {
                    "name": "Test",
                    "source_ids": ["shared"],
                    "notifications": {"enabled": False},
                },
                "interval_seconds": 3600,
            },
        )
        assert campaign.status_code == 201
        with sessions.begin() as session:
            session.add(
                OperationRow(
                    id=uuid4(),
                    kind="collection",
                    source_id="shared",
                    status="queued",
                    payload={"scheduled": True, "search_keywords": ["other"]},
                    result={},
                    created_at=now,
                )
            )
        runtime = OperatorRuntime(
            sessions,
            Configuration.model_validate({"runtime": {"manual_only": False}}),
            clock=FakeClock(now),
        )
        try:
            assert runtime.schedule_due() == 0
            with sessions() as session:
                operation = session.scalar(
                    select(OperationRow).where(OperationRow.kind == "collection")
                )
                assert operation is not None
                assert operation.payload["search_keywords"] == []
                assert operation.payload["account_campaign_ids"] == [campaign.json()["id"]]
            monitoring = client.get("/api/v1/account/campaigns").json()["items"][0]["monitoring"]
            assert monitoring["next_run_at"] is not None
            assert monitoring["last_operation_status"] == "queued"
            assert monitoring["last_run_at"] is None
        finally:
            runtime.close()


def test_ai_admin_endpoints_require_administrator(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    app = create_app(sessions, operator_mode=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/ai").status_code == 401
        register(client, "ai-user@example.com")
        assert client.get("/api/v1/admin/ai").status_code == 403
        assert (
            client.post(
                "/api/v1/admin/ai/providers",
                json={"name": "Denied", "base_url": "https://example.org/v1", "api_key": "secret"},
            ).status_code
            == 403
        )


def test_tender_details_keep_authorized_campaign_context(migrated_engine: Engine) -> None:
    sessions = session_factory(migrated_engine)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as operator:
        app = create_app(
            sessions, fixture.runtime.config, runtime=fixture.runtime, operator_mode=False
        )
        with TestClient(app) as client, TestClient(app) as other:
            register(client, "contexts@example.com")
            register(other, "unrelated@example.com")
            with sessions.begin() as session:
                session.add(SourceRow(id="manual", name="Manual", has_stable_external_id=True))
                verified_source(session, "manual")
            tender = operator.post(
                "/api/v1/web/tenders",
                json={
                    "request_id": str(uuid4()),
                    "title": "Network router",
                    "product_items": [{"name": "Network router"}],
                },
            ).json()
            campaigns = []
            for keyword in ("router", "concrete"):
                campaign = client.post(
                    "/api/v1/account/campaigns",
                    json={
                        "criteria": {
                            "name": keyword,
                            "source_ids": ["manual"],
                            "include_keywords": [keyword],
                            "notifications": {"enabled": False},
                        }
                    },
                ).json()
                campaigns.append(campaign)
                preview = operator.post(
                    "/api/v1/web/matching/preview",
                    json={
                        "campaign_id": campaign["id"],
                        "tender_ids": [tender["id"]],
                        "mode": "rules",
                    },
                ).json()
                reply = operator.post(f"/api/v1/web/matching/{preview['id']}/confirm", json={})
                assert reply.status_code == 202, reply.text
            assert client.get("/api/v1/account/tenders").json()["total"] == 2
            for campaign, relevance in zip(campaigns, ("RELEVANT", "NOT_RELEVANT"), strict=True):
                detail = client.get(
                    f"/api/v1/account/tenders/{tender['id']}",
                    params={"campaign_id": campaign["id"]},
                )
                assert detail.status_code == 200, detail.text
                assert detail.json()["campaign_id"] == campaign["id"]
                assert detail.json()["campaign_name"] == campaign["criteria"]["name"]
                assert detail.json()["relevance"] == relevance
                assert (
                    other.get(
                        f"/api/v1/account/tenders/{tender['id']}",
                        params={"campaign_id": campaign["id"]},
                    ).status_code
                    == 404
                )
            assert client.get(f"/api/v1/account/tenders/{tender['id']}").status_code == 409
            assert (
                client.get(
                    f"/api/v1/account/tenders/{tender['id']}", params={"campaign_id": str(uuid4())}
                ).status_code
                == 404
            )
