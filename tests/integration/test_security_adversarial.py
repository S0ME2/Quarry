"""Independent concurrent-account and upgrade adversarial checks; owned database only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from time import sleep
from typing import Any, cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from test_accounts import register, verified_source
from test_automatic_matching import campaign, collect, source

from tender_system.api.api import create_app
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import Configuration
from tender_system.operator.operations import OperatorService
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    AccountCampaignRow,
    AccountSourceRow,
    CampaignRow,
    MatchResultRow,
    OperationRow,
    SourceRow,
    TelegramNotificationRow,
    UserRow,
    session_factory,
)
from tender_system.testing.fakes import FakeClock
from tender_system.testing.web import web_fixture

pytestmark = pytest.mark.database


def test_concurrent_identical_custom_sources_create_one_request(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = session_factory(migrated_engine)
    with web_fixture(migrated_engine) as fixture:
        monkeypatch.setattr(fixture.runtime, "tick", lambda key: None)
        app = create_app(
            sessions, fixture.runtime.config, runtime=fixture.runtime, operator_mode=False
        )
        first, second = TestClient(app), TestClient(app)
        try:
            register(first, "duplicate-qa@example.org")
            second.cookies.update(first.cookies)

            def submit(client: TestClient) -> int:
                return client.post(
                    "/api/v1/account/sources",
                    json={"name": "Public source", "url": "https://tenders.example.org/list"},
                ).status_code

            with ThreadPoolExecutor(max_workers=2) as workers:
                assert sorted(workers.map(submit, (first, second))) == [201, 201]
            with sessions() as session:
                assert session.scalar(select(func.count()).select_from(AccountSourceRow)) == 1
                assert session.scalar(select(func.count()).select_from(OperationRow)) == 0
        finally:
            first.close()
            second.close()


def test_concurrent_custom_source_rechecks_do_not_duplicate_provider_work(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = session_factory(migrated_engine)
    with web_fixture(migrated_engine) as fixture:
        monkeypatch.setattr(fixture.runtime, "tick", lambda key: None)
        app = create_app(
            sessions, fixture.runtime.config, runtime=fixture.runtime, operator_mode=False
        )
        first, second = TestClient(app), TestClient(app)
        try:
            register(first, "recheck-qa@example.org")
            second.cookies.update(first.cookies)
            created = first.post(
                "/api/v1/account/sources",
                json={"name": "Recheck", "url": "https://tenders.example.org/recheck"},
            )
            assert created.status_code == 201, created.text
            key = created.json()["id"]
            original = OperatorService.enqueue

            def slow_enqueue(self: OperatorService, *args: Any, **kwargs: Any) -> Any:
                sleep(0.15)  # Expose the check/insert race without a provider request.
                return original(self, *args, **kwargs)

            monkeypatch.setattr(OperatorService, "enqueue", slow_enqueue)
            ready = Barrier(2)

            def submit(client: TestClient) -> int:
                ready.wait(timeout=5)
                return client.post(f"/api/v1/account/sources/{key}/check").status_code

            with ThreadPoolExecutor(max_workers=2) as workers:
                assert sorted(workers.map(submit, (first, second))) == [202, 409]
            with sessions() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(OperationRow)
                        .where(OperationRow.status == "queued")
                    )
                    == 1
                )
        finally:
            first.close()
            second.close()


def test_concurrent_administrators_cannot_remove_every_administrator(
    migrated_engine: Engine,
) -> None:
    sessions = session_factory(migrated_engine)
    app = create_app(sessions, operator_mode=False)
    clients = [TestClient(app), TestClient(app)]
    try:
        ids = [
            register(client, f"admin-qa-{n}@example.org")["id"] for n, client in enumerate(clients)
        ]
        with sessions.begin() as session:
            for row in session.scalars(select(UserRow)):
                row.role = "admin"

        def remove(index: int) -> int:
            return (
                clients[index]
                .patch(f"/api/v1/admin/users/{ids[1 - index]}", json={"active": False})
                .status_code
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            statuses = sorted(workers.map(remove, (0, 1)))
        assert statuses in ([200, 401], [200, 409]), statuses
        with sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(UserRow)
                    .where(UserRow.active, UserRow.role == "admin")
                )
                == 1
            )
    finally:
        for client in clients:
            client.close()


def test_running_shared_collection_does_not_consume_new_account_interval(
    migrated_engine: Engine,
) -> None:
    sessions = session_factory(migrated_engine)
    now = SystemClock().now()
    with sessions.begin() as session:
        session.add(SourceRow(id="shared-qa", name="Shared", has_stable_external_id=True))
        verified_source(session, "shared-qa")
    app = create_app(sessions, operator_mode=False)
    ids: list[str] = []
    for n in range(2):
        with TestClient(app) as client:
            register(client, f"interval-qa-{n}@example.org")
            reply = client.post(
                "/api/v1/account/campaigns",
                json={
                    "criteria": {
                        "name": "Shared",
                        "source_ids": ["shared-qa"],
                        "notifications": {"enabled": False},
                    },
                    "interval_seconds": 3600,
                },
            )
            assert reply.status_code == 201, reply.text
            ids.append(reply.json()["id"])
    with sessions.begin() as session:
        second = session.get(AccountCampaignRow, UUID(ids[1]))
        assert second is not None
        second.last_scheduled_at = now
    clock = FakeClock(now)
    runtime = OperatorRuntime(sessions, Configuration(), clock=clock)
    try:
        assert runtime.schedule_due() == 1
        with sessions.begin() as session:
            operation = session.scalar(
                select(OperationRow).where(OperationRow.kind == "collection")
            )
            assert operation is not None
            operation.status = "running"
        clock.instant += timedelta(hours=1)
        assert runtime.schedule_due() == 0
        with sessions.begin() as session:
            second = session.get(AccountCampaignRow, UUID(ids[1]))
            assert second is not None and second.last_scheduled_at == now
            operation = session.scalar(
                select(OperationRow).where(OperationRow.kind == "collection")
            )
            assert operation is not None
            assert operation.payload["account_campaign_ids"] == ids[:1]
            operation.status = "completed"
        assert runtime.schedule_due() == 1
        with sessions() as session:
            queued = session.scalar(select(OperationRow).where(OperationRow.status == "queued"))
            assert queued is not None
            assert set(cast(list[str], queued.payload["account_campaign_ids"])) == set(ids)
    finally:
        runtime.close()


def test_accounts_upgrade_preserves_existing_matches_and_pending_notifications(
    migrated_engine: Engine, migration_config: Config
) -> None:
    sessions = session_factory(migrated_engine)
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        created = campaign(client, notifications={"chat_id": "123"})
        key = source(client, fixture)
        collect(client, key)
        with sessions() as session:
            results_before = set(session.scalars(select(MatchResultRow.id)))
            queue_before = set(session.scalars(select(TelegramNotificationRow.id)))
            assert len(results_before) == len(queue_before) == 2
        # Return the fixture to the real previous schema, retaining its Phase1 data.
        with migrated_engine.begin() as connection:
            migration_config.attributes["connection"] = connection
            command.downgrade(migration_config, "0004_telegram_notifications")
            command.upgrade(migration_config, "head")
            command.check(migration_config)
        with sessions() as session:
            assert set(session.scalars(select(MatchResultRow.id))) == results_before
            assert set(session.scalars(select(TelegramNotificationRow.id))) == queue_before
            assert session.get(CampaignRow, UUID(created["id"])) is not None
            assert session.get(SourceRow, key) is not None
            assert session.scalar(select(func.count()).select_from(AccountCampaignRow)) == 0
            assert all(
                row.channel is None and row.status == "pending"
                for row in session.scalars(select(TelegramNotificationRow))
            )


def test_mixed_busy_sources_do_not_lose_an_accounts_due_cycle(migrated_engine: Engine) -> None:
    """A busy website cannot consume the interval for another selected website."""
    from uuid import uuid4

    sessions = session_factory(migrated_engine)
    now = SystemClock().now()
    with sessions.begin() as session:
        for key in ("available-qa", "running-qa"):
            session.add(SourceRow(id=key, name=key, has_stable_external_id=True))
        for key in ("available-qa", "running-qa"):
            verified_source(session, key)
        session.add(
            OperationRow(
                id=uuid4(),
                kind="collection",
                source_id="running-qa",
                status="running",
                payload={"scheduled": True},
                result={},
                created_at=now,
                started_at=now,
            )
        )
    with TestClient(create_app(sessions, operator_mode=False)) as client:
        register(client, "mixed-source-qa@example.org")
        reply = client.post(
            "/api/v1/account/campaigns",
            json={
                "criteria": {
                    "name": "Both websites",
                    "source_ids": ["available-qa", "running-qa"],
                    "notifications": {"enabled": False},
                },
                "interval_seconds": 3600,
            },
        )
        assert reply.status_code == 201, reply.text
        campaign_id = reply.json()["id"]
    clock = FakeClock(now)
    runtime = OperatorRuntime(sessions, Configuration(), clock=clock)
    try:
        runtime.schedule_due()
        with sessions.begin() as session:
            for row in session.scalars(
                select(OperationRow).where(OperationRow.status == "running")
            ):
                row.status = "completed"
        runtime.schedule_due()
        with sessions() as session:
            queued = list(
                session.scalars(select(OperationRow).where(OperationRow.status == "queued"))
            )
            assert any(
                row.source_id == "running-qa"
                and campaign_id in cast(list[str], row.payload.get("account_campaign_ids", []))
                for row in queued
            ), "Due campaign lost its previously busy website until the next interval"
    finally:
        runtime.close()
