"""Account intervals enqueue existing collection work, sharing source executions."""

from datetime import datetime, timedelta
from typing import cast

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_system.core.domain import CampaignCriteria
from tender_system.operator.operations import OperatorService
from tender_system.operator.source_approval import eligible
from tender_system.persistence.storage import (
    AccountCampaignRow,
    CampaignRow,
    CriteriaRow,
    OperationRow,
    UserRow,
)


def schedule_accounts(session: Session, svc: OperatorService, now: datetime) -> int:
    count = 0
    owners = session.scalars(
        select(AccountCampaignRow)
        .join(UserRow)
        .join(CampaignRow, CampaignRow.id == AccountCampaignRow.campaign_id)
        .where(
            UserRow.active.is_(True), CampaignRow.active.is_(True), CampaignRow.deleted_at.is_(None)
        )
        .order_by(AccountCampaignRow.campaign_id)
    )
    for owner in owners:
        if (
            owner.last_scheduled_at
            and owner.last_scheduled_at + timedelta(seconds=owner.interval_seconds) > now
        ):
            continue
        campaign = session.get(CampaignRow, owner.campaign_id)
        assert campaign is not None
        version = session.get(CriteriaRow, campaign.current_criteria_version_id)
        assert version is not None
        criteria = CampaignCriteria.model_validate(version.criteria)
        # A campaign interval covers its whole source selection. Do not advance it
        # after a partial enqueue while another selected source is already running.
        # Lock queued work so a worker cannot start between this check and merging targets.
        busy_sources = list(
            session.scalars(
                select(OperationRow)
                .where(
                    OperationRow.source_id.in_(criteria.source_ids),
                    OperationRow.kind == "collection",
                    OperationRow.status.in_(("queued", "running")),
                )
                .order_by(OperationRow.id)
                .with_for_update()
            )
        )
        if any(operation.status == "running" for operation in busy_sources):
            continue
        scheduled = False
        for key in criteria.source_ids:
            control = svc.control(key)
            if not eligible(session, key):
                continue
            busy = session.scalar(
                select(OperationRow)
                .where(
                    OperationRow.source_id == key,
                    OperationRow.kind == "collection",
                    OperationRow.status.in_(("queued", "running")),
                )
                .limit(1)
            )
            if busy is not None:
                if busy.status == "running":
                    continue
                targets = cast(list[JsonValue], busy.payload.get("account_campaign_ids", []))
                busy.payload = {
                    **busy.payload,
                    "account_campaign_ids": [*targets, str(owner.campaign_id)],
                    # Existing source work can be restricted to unrelated campaign
                    # keywords. Account campaigns require an unrestricted scrape.
                    "search_keywords": [],
                }
            else:
                payload: dict[str, JsonValue] = {
                    "scheduled": True,
                    "account_scheduled": True,
                    "account_campaign_ids": [str(owner.campaign_id)],
                    "search_keywords": [],
                }
                limits = control.get("approved_execution_limits")
                if isinstance(limits, dict):
                    payload["limits"] = limits
                svc.enqueue("collection", key, payload)
                count += 1
            scheduled = True
        if scheduled:
            owner.last_scheduled_at = now
    return count
