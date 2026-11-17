"""Account-preserving rebuild and compatibility with the previous account-free schema."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session
from test_database_rebuild import legacy as legacy
from test_database_rebuild import target as target

from tender_system.api.auth import password_hash, token_hash
from tender_system.core.clock import SystemClock
from tender_system.core.domain import CampaignCriteria
from tender_system.persistence.storage import (
    AccountCampaignRow,
    AccountSourceRow,
    CampaignRow,
    CriteriaRow,
    SourceRow,
    TenderRow,
    UserRow,
    UserSessionRow,
)
from tools.database_rebuild import import_database

pytestmark = pytest.mark.database


def test_rebuild_retains_account_session_and_private_resource_ownership(
    legacy: Engine, target: Engine
) -> None:
    now = SystemClock().now()
    user_id, campaign_id, criteria_id = uuid4(), uuid4(), uuid4()
    digest = password_hash("synthetic-rebuild-fixture-password")
    session_hash = token_hash("synthetic-rebuild-fixture-session")
    criteria = CampaignCriteria(name="Private campaign", source_ids=("fixture",))
    with Session(legacy) as session, session.begin():
        session.add(
            UserRow(
                id=user_id,
                email="rebuild-qa@example.org",
                password_hash=digest,
                role="user",
                active=True,
                created_at=now,
                failed_logins=0,
            )
        )
        session.flush()
        session.add(
            UserSessionRow(
                token_hash=session_hash,
                user_id=user_id,
                expires_at=now + timedelta(hours=12),
            )
        )
        session.add(
            CampaignRow(
                id=campaign_id,
                active=True,
                current_criteria_version_id=criteria_id,
            )
        )
        session.flush()
        session.add(
            CriteriaRow(
                id=criteria_id,
                campaign_id=campaign_id,
                number=1,
                previous_version_id=None,
                criteria=criteria.model_dump(mode="json"),
                created_at=now,
            )
        )
        session.flush()
        session.add(
            AccountCampaignRow(
                campaign_id=campaign_id,
                user_id=user_id,
                interval_seconds=10800,
                last_scheduled_at=now,
            )
        )
        session.add(AccountSourceRow(source_id="fixture", user_id=user_id))
    report = import_database(legacy, target)
    assert all(
        report["tables"][name]["count"] == 1
        for name in (
            "users",
            "user_sessions",
            "account_campaigns",
            "account_sources",
        )
    )
    with Session(target) as session:
        user = session.get(UserRow, user_id)
        assert user is not None and user.password_hash == digest and user.role == "user"
        login = session.get(UserSessionRow, session_hash)
        assert login is not None and login.user_id == user_id and login.expires_at > now
        campaign = session.get(AccountCampaignRow, campaign_id)
        assert campaign is not None and campaign.user_id == user_id
        assert campaign.interval_seconds == 10800 and campaign.last_scheduled_at == now
        source = session.get(AccountSourceRow, "fixture")
        assert source is not None and source.user_id == user_id
        assert session.get(SourceRow, "fixture") is not None
        assert session.scalar(select(func.count()).select_from(TenderRow)) == 1


def test_rebuild_accepts_pre_account_source_without_nullable_notification_columns(
    legacy: Engine, target: Engine
) -> None:
    with legacy.begin() as connection:
        for table in ("account_campaigns", "account_sources", "user_sessions", "users"):
            connection.execute(text(f'DROP TABLE "{table}"'))
        connection.execute(text("ALTER TABLE telegram_notifications DROP COLUMN channel"))
        connection.execute(text("ALTER TABLE telegram_notifications DROP COLUMN destination"))
    report = import_database(legacy, target)
    assert report["history"]["tenders"] == 1
    assert report["history"]["versions"] == 2
    assert all(
        report["tables"][name]["count"] == 0
        for name in (
            "users",
            "user_sessions",
            "account_campaigns",
            "account_sources",
        )
    )
    with Session(target) as session:
        assert session.get(SourceRow, "fixture") is not None
        assert session.scalar(select(func.count()).select_from(TenderRow)) == 1
        assert session.scalar(select(func.count()).select_from(UserRow)) == 0
