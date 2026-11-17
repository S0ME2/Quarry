"""Durable, bounded Telegram delivery for automatic matching."""

import os
import smtplib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from tender_system.core.domain import CampaignCriteria, MatchResult, StoredMatchResult
from tender_system.operator.email_delivery import EmailConfigurationError, send_email
from tender_system.persistence.storage import (
    AccountCampaignRow,
    CampaignRow,
    CriteriaRow,
    MatchResultRow,
    MatchSelectionRow,
    SourceRow,
    TelegramNotificationRow,
    TenderRow,
    UserRow,
)


def _short(value: str, limit: int) -> str:
    # Telegram entity offsets use UTF-16; bound even astral characters conservatively.
    return value.encode("utf-16-le")[: limit * 2].decode("utf-16-le", errors="ignore")


def enqueue_notification(session: Session, result: StoredMatchResult, now: datetime) -> None:
    tender = session.get(TenderRow, result.tender_id)
    criteria = session.get(CriteriaRow, result.criteria_version_id)
    assert tender is not None and criteria is not None
    settings = CampaignCriteria.model_validate(criteria.criteria).notifications
    if not settings.enabled or result.outcome.relevance != "RELEVANT":
        return
    source = session.get(SourceRow, tender.source_id)
    reasons = "; ".join(reason.detail for reason in result.outcome.reasons) or "Relevant match"
    price = str(tender.value) if tender.value is not None else "Not available"
    deadline = tender.deadline.astimezone(UTC).isoformat() if tender.deadline else "Not available"
    message = "\n".join(
        (
            f"Campaign: {_short(str(criteria.criteria.get('name', result.campaign_id)), 150)}",
            f"Tender: {_short(tender.title or 'Not available', 350)}",
            f"Buyer: {_short(tender.buyer or 'Not available', 250)}",
            f"Price: {_short(price, 100)}",
            f"Currency: {_short(tender.currency or 'Not available', 30)}",
            f"Deadline: {deadline}",
            f"Platform: {_short(source.name if source else tender.source_id, 150)}",
            f"Relevance: {_short(reasons, 400)}",
            f"Link: {_short(tender.source_url or 'Not available', 1500)}",
        )
    )
    session.execute(
        insert(TelegramNotificationRow)
        .values(
            id=uuid4(),
            campaign_id=result.campaign_id,
            tender_id=result.tender_id,
            tender_version_id=result.tender_version_id,
            result_id=result.id,
            message=message,
            channel=settings.channel,
            destination=settings.email if settings.channel == "email" else settings.chat_id,
            status="pending",
            attempts=0,
            available_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_telegram_match",
            set_={
                "result_id": result.id,
                "message": message,
                "channel": settings.channel,
                "destination": settings.email if settings.channel == "email" else settings.chat_id,
                "status": "pending",
                "available_at": now,
                "last_error": None,
            },
            # Replace obsolete unsent work; never resend delivered or ambiguous work.
            where=TelegramNotificationRow.status.in_(("pending", "superseded")),
        )
    )


def dispatch_notifications(sessions: sessionmaker[Session], now: datetime) -> None:
    token = os.environ.get("QUARRY_TELEGRAM_BOT_TOKEN")
    default_chat_id = os.environ.get("QUARRY_TELEGRAM_CHAT_ID")
    for _ in range(20):
        with sessions.begin() as session:
            row = session.scalar(
                select(TelegramNotificationRow)
                .where(
                    TelegramNotificationRow.status == "pending",
                    TelegramNotificationRow.available_at <= now,
                    # Unconfigured Telegram must not delay work before credentials appear,
                    # while independently configured email delivery continues.
                    or_(TelegramNotificationRow.channel == "email", literal(bool(token))),
                )
                .order_by(TelegramNotificationRow.available_at, TelegramNotificationRow.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return
            selected = session.get(MatchSelectionRow, (row.campaign_id, row.tender_id))
            result = session.get(MatchResultRow, row.result_id)
            campaign = session.get(CampaignRow, row.campaign_id)
            tender = session.get(TenderRow, row.tender_id)
            if (
                selected is None
                or result is None
                or selected.job_id != result.job_id
                or campaign is None
                or not campaign.active
                or campaign.deleted_at is not None
                or campaign.current_criteria_version_id != result.criteria_version_id
                or tender is None
                or tender.current_version_id != row.tender_version_id
            ):
                row.status = "superseded"
                row.last_error = "Match selection changed before delivery"
                continue
            outcome = MatchResult.model_validate(result.outcome)
            if outcome.relevance != "RELEVANT":
                row.status, row.last_error = "superseded", "Result is not a verified match"
                continue
            criteria = session.get(CriteriaRow, campaign.current_criteria_version_id)
            assert criteria is not None
            ownership = session.get(AccountCampaignRow, campaign.id)
            if ownership:
                owner = session.get(UserRow, ownership.user_id)
                if owner is None or not owner.active:
                    row.status, row.last_error = "superseded", "Account is unavailable"
                    continue
            settings = CampaignCriteria.model_validate(criteria.criteria).notifications
            if not settings.enabled:
                row.status, row.last_error = "superseded", "Notifications disabled"
                continue
            channel = row.channel or "telegram"
            configured = settings.email if channel == "email" else settings.chat_id
            if settings.channel != channel or (row.destination and row.destination != configured):
                row.status, row.last_error = "superseded", "Notification destination changed"
                continue
            destination = (
                row.destination
                or configured
                or (default_chat_id if channel == "telegram" and ownership is None else None)
            )
            if not destination or (channel == "telegram" and not token):
                row.available_at = now + timedelta(seconds=60)
                row.last_error = "Notification transport or destination not configured"
                continue
            key, message = row.id, row.message
            row.status = "uncertain"
            row.attempts += 1
            row.last_error = "Delivery outcome unknown"
        if channel == "email":
            status, error = "delivered", None
            try:
                from tender_system.operator.smtp_settings import delivery_config

                with sessions() as session:
                    smtp_config = delivery_config(session)
                if smtp_config is None:
                    send_email(destination, message)
                else:
                    send_email(destination, message, smtp_config)
            except (EmailConfigurationError, ValueError):
                status, error = "pending", "Email transport is not configured correctly"
            except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused):
                status, error = "pending", "Email server rejected delivery"
            except (OSError, smtplib.SMTPException):
                status, error = "uncertain", "Email delivery outcome unknown"
            with sessions.begin() as session:
                row = session.get(TelegramNotificationRow, key)
                assert row is not None
                row.status, row.last_error = status, error
                if status == "delivered":
                    row.delivered_at = now
                elif status == "pending":
                    row.available_at = now + timedelta(
                        seconds=min(3600, 60 * 2 ** min(row.attempts - 1, 6))
                    )
            continue
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": destination, "text": message},
                timeout=10.0,
            )
            try:
                data = response.json()
            except ValueError:
                data = {}
            success = response.is_success and isinstance(data, dict) and data.get("ok") is True
            # A valid Telegram rejection confirms that no message was accepted.
            rejected = isinstance(data, dict) and data.get("ok") is False
        except httpx.RequestError:
            continue
        with sessions.begin() as session:
            row = session.get(TelegramNotificationRow, key)
            assert row is not None
            if success:
                row.status, row.delivered_at, row.last_error = "delivered", now, None
            elif rejected:
                row.status = "pending"
                parameters = data.get("parameters", {})
                retry_after = (
                    parameters.get("retry_after") if isinstance(parameters, dict) else None
                )
                row.available_at = now + timedelta(
                    seconds=max(
                        min(3600, 60 * 2 ** min(row.attempts - 1, 6)),
                        retry_after if type(retry_after) is int and retry_after > 0 else 0,
                    )
                )
                row.last_error = f"Telegram rejected request (HTTP {response.status_code})"
            else:
                row.last_error = f"Telegram response uncertain (HTTP {response.status_code})"
