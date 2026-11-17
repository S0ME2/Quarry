"""Persisted delivery cannot escape an account through legacy destination defaults."""

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from test_automatic_matching import campaign, collect, source

from tender_system.core.clock import SystemClock
from tender_system.operator.telegram import dispatch_notifications
from tender_system.persistence.storage import (
    AccountCampaignRow,
    TelegramNotificationRow,
    UserRow,
)
from tender_system.testing.web import web_fixture

pytestmark = pytest.mark.database


@pytest.mark.parametrize("active", [True, False])
def test_owned_queued_notifications_never_use_operator_destination(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        created = campaign(client)
        collect(client, source(client, fixture))
        with fixture.runtime.sessions.begin() as session:
            user = UserRow(
                id=uuid4(),
                email="isolation@example.org",
                password_hash="unused-fixture",
                role="user",
                active=active,
                created_at=SystemClock().now(),
                failed_logins=0,
            )
            session.add(user)
            session.flush()
            session.add(
                AccountCampaignRow(
                    campaign_id=UUID(created["id"]),
                    user_id=user.id,
                    interval_seconds=3600,
                )
            )
            for row in session.scalars(select(TelegramNotificationRow)):
                row.available_at = fixture.runtime.clock.now()
        monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "123")

        def forbidden(*args: object, **kwargs: object) -> httpx.Response:
            pytest.fail("Private account notification escaped to operator destination")

        monkeypatch.setattr(httpx, "post", forbidden)
        dispatch_notifications(fixture.runtime.sessions, fixture.runtime.clock.now())
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert len(rows) == 2
            assert all(row.status == ("pending" if active else "superseded") for row in rows)
            assert all(row.attempts == 0 for row in rows)
