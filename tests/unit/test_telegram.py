"""Notification configuration validation and SRS message bounds."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from tender_system.core.domain import (
    CampaignCriteria,
    MatchResult,
    NotificationSettings,
    Reason,
    StoredMatchResult,
)
from tender_system.operator.telegram import enqueue_notification
from tender_system.persistence.storage import CriteriaRow, SourceRow, TenderRow


@pytest.mark.parametrize("chat_id", ["123", "-1001234567890", "@quarry_channel", None])
def test_notification_destination(chat_id: str | None) -> None:
    assert NotificationSettings(chat_id=chat_id).chat_id == chat_id


@pytest.mark.parametrize(
    "settings",
    [
        {"chat_id": ""},
        {"chat_id": " 123"},
        {"chat_id": "123:bot-token"},
        {"chat_id": "https://example.org"},
        {"channel": "email"},
        {"bot_token": "secret"},
    ],
)
def test_notification_configuration_rejects_invalid_fields(settings: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        NotificationSettings.model_validate(settings)


def test_legacy_campaign_notification_defaults() -> None:
    criteria = CampaignCriteria.model_validate({"name": "Legacy"})
    assert criteria.notifications == NotificationSettings()
    assert criteria.model_dump(mode="json")["notifications"] == {
        "enabled": True,
        "channel": "telegram",
        "chat_id": None,
        "email": None,
    }


@pytest.mark.parametrize("decision", ["match", "no_match", "unsure", "invalid", "technical"])
@pytest.mark.parametrize("offset_hours", [0, 5, -8])
def test_notification_content_and_relevance_guard(decision: str, offset_hours: int) -> None:
    now = datetime(2026, 11, 4, tzinfo=UTC)
    tender_id, criteria_id = uuid4(), uuid4()
    outcome = MatchResult.model_validate(
        {
            "result": decision if decision in {"no_match", "unsure"} else "match",
            "score": 1,
            "reasons": [
                Reason(
                    code="keyword", detail="Relevant network equipment " + "🚀" * 3000
                ).model_dump()
            ],
            "uncertainties": [],
            "evidence_verification": {"status": "invalid"} if decision == "invalid" else None,
            "model_calls": [
                {"stage": "semantic", "provider": "fake", "model": "fake", "error_code": "failed"}
            ]
            if decision == "technical"
            else [],
        }
    )
    result = StoredMatchResult(
        id=uuid4(),
        job_id=uuid4(),
        tender_id=tender_id,
        tender_version_id=uuid4(),
        campaign_id=uuid4(),
        criteria_version_id=criteria_id,
        matcher_implementation="rules",
        matcher_version="1",
        settings_hash="hash",
        outcome=outcome,
        created_at=now,
        completion_token=uuid4(),
    )
    rows = {
        TenderRow: TenderRow(
            id=tender_id,
            title="🚀" * 3000,
            buyer="🚀" * 3000,
            value=Decimal("120000"),
            currency="USD",
            deadline=now.astimezone(timezone(timedelta(hours=offset_hours))),
            source_id="fixture",
            source_url="https://example.org/tender/123",
        ),
        CriteriaRow: CriteriaRow(criteria={"name": "🚀" * 3000}),
        SourceRow: SourceRow(name="🚀" * 3000),
    }
    session = Mock(spec=Session)
    session.get.side_effect = lambda row, key: rows[row]
    enqueue_notification(session, result, now)
    if decision != "match":
        session.execute.assert_not_called()
        return
    message = session.execute.call_args.args[0].compile().params["message"]
    assert len(message.encode("utf-16-le")) // 2 <= 4096
    assert "Price: 120000" in message and "Currency: USD" in message
    assert f"Deadline: {now.isoformat()}" in message
    assert "Relevance: Relevant network equipment" in message
    assert message.endswith("Link: https://example.org/tender/123")
    assert all(f"{field}: " in message for field in ("Tender", "Buyer", "Platform", "Campaign"))
