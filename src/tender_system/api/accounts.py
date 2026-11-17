"""Self-service projections use explicit account ownership at every resource boundary."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from pydantic import Field, JsonValue
from sqlalchemy import Select, delete, func, or_, select, text
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.orm import Session

from tender_system.api.auth import UserView, user_view
from tender_system.core.clock import SystemClock
from tender_system.core.configuration import Configuration
from tender_system.core.domain import CampaignCriteria, Contract, ExecutionLimits, MatchResult
from tender_system.matching.matching import CampaignService, MatchingPlanner
from tender_system.operator.operations import OperatorService, sanitized
from tender_system.operator.operator_contracts import SourceURL
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.operator.source_approval import decision, eligible, verified
from tender_system.persistence.matching_storage import (
    SqlCampaignRepository,
    SqlMatchJobRepository,
    catalog_lock,
)
from tender_system.persistence.storage import (
    AccountCampaignRow,
    AccountSourceRow,
    ActiveRecipeRow,
    CampaignRow,
    CriteriaRow,
    MatchJobRow,
    MatchResultRow,
    MatchSelectionRow,
    OperationRow,
    OperatorConfigRow,
    SourceRow,
    SqlTenderRepository,
    TelegramNotificationRow,
    TenderDocumentRow,
    TenderRow,
    UserRow,
    UserSessionRow,
)


class AccountCampaignWrite(Contract):
    criteria: CampaignCriteria
    active: bool = True
    interval_seconds: int = Field(default=3600, ge=60, le=604800)
    expected_version_id: UUID | None = None


class CustomSource(SourceURL):
    name: str = Field(min_length=1, max_length=200)


class UserUpdate(Contract):
    active: bool | None = None
    role: Literal["user", "admin"] | None = None


class SourceDecision(Contract):
    decision: Literal["approved", "rejected"]


class SourceResolution(Contract):
    existing_source_id: str | None = None


def normalized_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = "[" + host + "]"
    if parsed.port and (parsed.scheme.lower(), parsed.port) not in {("http", 80), ("https", 443)}:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))


def register_accounts(
    app: FastAPI,
    database: Callable[..., Iterator[Session]],
    current_user: Callable[..., UserRow],
    config: Configuration,
    runtime: OperatorRuntime | None,
) -> None:
    Database = Annotated[Session, Depends(database, scope="function")]
    CurrentUser = Annotated[UserRow, Depends(current_user)]
    prefix = "/api/v1/account"

    def service(session: Session) -> OperatorService:
        return OperatorService(session, config, SystemClock().now())

    def campaign_service(svc: OperatorService) -> CampaignService:
        catalog_lock(svc.session)
        repo = SqlCampaignRepository(svc.session)
        return CampaignService(
            repo,
            MatchingPlanner(
                repo,
                SqlMatchJobRepository(svc.session),
                SqlTenderRepository(svc.session),
                svc.config,
                automatic=False,
            ),
            SystemClock(),
        )

    def campaign_owner(session: Session, key: UUID, user: UserRow) -> AccountCampaignRow:
        owner = session.get(AccountCampaignRow, key)
        if owner is None or owner.user_id != user.id:
            raise HTTPException(404)
        return owner

    def source_view(session: Session, key: str, custom: bool) -> dict[str, Any]:
        svc = service(session)
        metadata = session.get(OperatorConfigRow, "web/source/" + key)
        source = session.get(SourceRow, key)
        control = svc.control(key)
        manual_only = control.get("manual_only", False)
        review = decision(session, key)
        status = (
            "REJECTED"
            if review == "rejected"
            else "UNDER_REVIEW"
            if review == "pending"
            else "APPROVED"
            if eligible(session, key)
            else "NEEDS_ATTENTION"
        )
        if metadata:
            name, url = metadata.value.get("name", key), metadata.value.get("url", "")
        elif source:
            view = svc.source_view(source)
            name, url = source.name, view.get("url", "")
        else:
            name, url = key, ""
        return {
            "id": key,
            "name": name,
            "url": url,
            "status": status,
            "custom": custom,
            "source_manual_only": bool(manual_only),
            "global_manual_only": svc.config.runtime.manual_only,
            "error": (
                "This website needs administrator attention"
                if status == "NEEDS_ATTENTION"
                else None
            ),
        }

    def available_source(session: Session, key: str, user: UserRow) -> None:
        if not eligible(session, key) or session.get(SourceRow, key) is None:
            raise HTTPException(404)

    @app.get(prefix + "/sources")
    def sources(user: CurrentUser, session: Database) -> dict[str, Any]:
        catalog = session.scalars(select(SourceRow.id)).all()
        owned = session.scalars(
            select(AccountSourceRow.source_id).where(AccountSourceRow.user_id == user.id)
        ).all()
        requests = session.scalars(
            select(OperatorConfigRow).where(
                OperatorConfigRow.key.like(f"source-request/{user.id}/%")
            )
        )
        requested = set(owned) | {row.key.rsplit("/", 1)[-1] for row in requests}
        available = {key for key in catalog if eligible(session, key)}
        return {
            "items": [
                source_view(session, key, key in requested) for key in sorted(available | requested)
            ]
        }

    def enqueue_check(session: Session, key: str, background: BackgroundTasks) -> None:
        if runtime is None:
            raise HTTPException(409, "Website worker unavailable")
        svc = service(session)
        if session.scalar(
            select(OperationRow.id)
            .where(OperationRow.source_id == key, OperationRow.status.in_(("queued", "running")))
            .limit(1)
        ):
            raise HTTPException(409, "Website check already running")
        operation = svc.enqueue(
            "recipe_generation",
            key,
            {
                "limits": ExecutionLimits(
                    max_tenders=3,
                    max_requests=100,
                    max_duration_seconds=min(360, config.runtime.operation_timeout_seconds),
                    max_response_bytes=2_000_000,
                    max_total_response_bytes=10_000_000,
                ).model_dump(mode="json")
            },
        )
        background.add_task(runtime.tick, operation.id)

    @app.post(prefix + "/sources", status_code=201)
    def add_source(
        body: CustomSource, user: CurrentUser, session: Database, background: BackgroundTasks
    ) -> dict[str, Any]:
        try:
            url = normalized_url(body.url)
        except ValueError:
            raise HTTPException(422, "Invalid website URL") from None
        # Serialize lookup and creation: different accounts can request the same URL.
        session.execute(text("select pg_advisory_xact_lock(5189062794521021)"))
        svc = service(session)
        key = None
        for existing in session.scalars(
            select(OperatorConfigRow).where(OperatorConfigRow.key.like("web/source/%"))
        ):
            existing_url = existing.value.get("url")
            try:
                same = isinstance(existing_url, str) and normalized_url(existing_url) == url
            except ValueError:
                same = False
            if same:
                key = str(
                    existing.value.get("resolved_source_id")
                    or existing.key.removeprefix("web/source/")
                )
                break
        if key is None:
            # A legacy catalog item without a public URL cannot be identified by
            # URL. A matching name is only a warning, never proof of identity.
            candidates: list[JsonValue] = []
            for source in session.scalars(select(SourceRow)):
                metadata = session.get(OperatorConfigRow, "web/source/" + source.id)
                if source.name.strip().casefold() == body.name.strip().casefold() and (
                    metadata is None or not metadata.value.get("url")
                ):
                    candidates.append(source.id)
            key = "source-" + uuid4().hex
            svc.write(
                "web/source/" + key,
                {
                    "source_id": key,
                    "name": body.name,
                    "url": url,
                    "identity_candidates": candidates,
                },
            )
            svc.write("source/" + key, {"enabled": True, "manual_only": False})
            svc.write("source-review/" + key, {"decision": "pending"})
            session.add(AccountSourceRow(source_id=key, user_id=user.id))
        svc.write(f"source-request/{user.id}/{key}", {"source_id": key})
        session.flush()
        return source_view(session, key, True)

    def request_view(session: Session, key: str) -> dict[str, Any]:
        metadata = session.get(OperatorConfigRow, "web/source/" + key)
        control = service(session).control(key)
        active = session.get(ActiveRecipeRow, key)
        candidates = metadata.value.get("identity_candidates") if metadata else None
        latest = session.scalar(
            select(OperationRow)
            .where(OperationRow.source_id == key, OperationRow.kind == "recipe_generation")
            .order_by(OperationRow.created_at.desc())
            .limit(1)
        )
        return {
            "id": key,
            "name": metadata.value.get("name", key) if metadata else key,
            "url": metadata.value.get("url", "") if metadata else "",
            "decision": decision(session, key),
            "technical_verified": verified(session, key),
            "active_recipe_version": active.version if active else None,
            "enabled": bool(control.get("enabled", True)),
            "manual_only": bool(control.get("manual_only", False)),
            "identity_candidates": [
                {"id": candidate.id, "name": candidate.name}
                for candidate_id in (candidates if isinstance(candidates, list) else [])
                if isinstance(candidate_id, str)
                if (candidate := session.get(SourceRow, candidate_id)) is not None
            ],
            "latest_operation": {
                "id": str(latest.id),
                "status": latest.status,
                "error_code": latest.error_code,
                "result": sanitized(latest.result),
            }
            if latest
            else None,
        }

    @app.get("/api/v1/admin/source-requests")
    def source_requests(session: Database) -> dict[str, Any]:
        rows = session.scalars(
            select(OperatorConfigRow)
            .where(OperatorConfigRow.key.like("source-review/%"))
            .order_by(OperatorConfigRow.updated_at.desc())
        ).all()
        keys = {row.key.removeprefix("source-review/") for row in rows}
        keys.update(session.scalars(select(AccountSourceRow.source_id)))
        return {"items": [request_view(session, key) for key in sorted(keys)]}

    @app.post("/api/v1/admin/source-requests/{key}/resolve")
    def resolve_source(key: str, body: SourceResolution, session: Database) -> dict[str, Any]:
        session.execute(text("select pg_advisory_xact_lock(5189062794521021)"))
        metadata = session.get(OperatorConfigRow, "web/source/" + key)
        candidates = metadata.value.get("identity_candidates") if metadata else None
        if not metadata or not isinstance(candidates, list) or not candidates:
            raise HTTPException(409, "No unresolved identity review")
        svc = service(session)
        if body.existing_source_id is None:
            svc.write(metadata.key, {**metadata.value, "identity_candidates": []})
            session.flush()
            return request_view(session, key)
        target = body.existing_source_id
        target_source = session.get(SourceRow, target)
        if target not in candidates or target_source is None:
            raise HTTPException(422, "Source is not an identity candidate")
        target_metadata = session.get(OperatorConfigRow, "web/source/" + target)
        if target_metadata and target_metadata.value.get("url"):
            raise HTTPException(409, "Existing source already has a website URL")
        if session.get(SourceRow, key) or session.scalar(
            select(OperationRow.id).where(OperationRow.source_id == key).limit(1)
        ):
            raise HTTPException(409, "Request already has technical records")
        svc.write(
            "web/source/" + target,
            {
                **(target_metadata.value if target_metadata else {}),
                "source_id": target,
                "name": target_source.name,
                "url": metadata.value["url"],
            },
        )
        svc.write(
            metadata.key,
            {**metadata.value, "identity_candidates": [], "resolved_source_id": target},
        )
        for request in session.scalars(
            select(OperatorConfigRow).where(OperatorConfigRow.key.like("source-request/%/" + key))
        ).all():
            user_id = request.key.split("/")[1]
            svc.write(f"source-request/{user_id}/{target}", {"source_id": target})
            session.delete(request)
        owner = session.get(AccountSourceRow, key)
        if owner:
            session.delete(owner)
        review = session.get(OperatorConfigRow, "source-review/" + key)
        if review:
            session.delete(review)
        session.flush()
        return request_view(session, target)

    @app.patch("/api/v1/admin/source-requests/{key}")
    def review_source(key: str, body: SourceDecision, session: Database) -> dict[str, Any]:
        from tender_system.persistence.recipe_storage import source_lock

        source_lock(session, key)
        metadata = session.get(OperatorConfigRow, "web/source/" + key)
        if metadata and metadata.value.get("identity_candidates"):
            raise HTTPException(409, "Resolve possible duplicate before deciding")
        if (
            session.get(OperatorConfigRow, "source-review/" + key) is None
            and session.get(AccountSourceRow, key) is None
        ):
            raise HTTPException(404, "Source request unavailable")
        service(session).write("source-review/" + key, {"decision": body.decision})
        session.flush()
        return request_view(session, key)

    @app.post(prefix + "/sources/{key}/check", status_code=202)
    def check_source(
        key: str, user: CurrentUser, session: Database, background: BackgroundTasks
    ) -> dict[str, Any]:
        owner = session.scalar(
            select(AccountSourceRow)
            .where(AccountSourceRow.source_id == key, AccountSourceRow.user_id == user.id)
            .with_for_update()
        )
        if owner is None:
            raise HTTPException(404)
        metadata = session.get(OperatorConfigRow, "web/source/" + key)
        if metadata and metadata.value.get("identity_candidates"):
            raise HTTPException(409, "Website identity needs administrator review")
        if decision(session, key) != "pending":
            raise HTTPException(409, "Website review is already complete")
        if session.get(ActiveRecipeRow, key):
            return source_view(session, key, True)
        enqueue_check(session, key, background)
        return source_view(session, key, True)

    def campaign_view(session: Session, owner: AccountCampaignRow) -> dict[str, Any]:
        campaign = service(session).campaign(owner.campaign_id)
        jobs = list(
            session.scalars(
                select(MatchJobRow)
                .where(
                    MatchJobRow.campaign_id == owner.campaign_id,
                    MatchJobRow.criteria_version_id == campaign.current_criteria_version_id,
                )
                .order_by(MatchJobRow.created_at.desc())
            )
        )
        pending = sum(j.status in {"pending", "running"} for j in jobs)
        failed = sum(j.status == "failed" for j in jobs)
        error = next((j.last_error_code for j in jobs if j.last_error_code), None)
        last_operation = session.scalar(
            select(OperationRow)
            .where(
                or_(
                    OperationRow.payload["account_campaign_ids"].contains([str(owner.campaign_id)]),
                    OperationRow.payload["campaign_ids"].contains([str(owner.campaign_id)]),
                )
            )
            .order_by(OperationRow.created_at.desc())
            .limit(1)
        )
        if error is None and last_operation is not None:
            error = last_operation.error_code
            stopped = last_operation.result.get("stopped")
            if isinstance(stopped, str):
                error = stopped
        monitoring_status = (
            "blocked"
            if error and pending
            else "failed"
            if failed
            else "running"
            if pending
            else "scheduled"
            if last_operation and last_operation.status in {"queued", "running"}
            else "failed"
            if last_operation and last_operation.status in {"failed", "escalated"}
            else "completed"
            if jobs
            else "idle"
        )
        scopes = []
        for source_id in campaign.criteria.source_ids:
            collection = session.scalar(
                select(OperationRow)
                .where(
                    OperationRow.kind == "collection",
                    OperationRow.source_id == source_id,
                    OperationRow.payload["account_campaign_ids"].contains([str(owner.campaign_id)]),
                )
                .order_by(OperationRow.created_at.desc(), OperationRow.id)
                .limit(1)
            )
            control = service(session).control(source_id)
            limits = control.get("approved_execution_limits")
            scopes.append(
                {
                    "source_id": source_id,
                    "max_tenders": limits.get("max_tenders") if isinstance(limits, dict) else None,
                    "scope_approved": isinstance(limits, dict),
                    "coverage_status": collection.result.get("coverage_status", "unknown")
                    if collection
                    else "unknown",
                    "collection_status": collection.result.get("collection_status")
                    if collection
                    else None,
                    "records_processed": collection.result.get("records_processed")
                    if collection
                    else None,
                }
            )
        coverage = (
            "incomplete"
            if any(
                s["coverage_status"] == "incomplete" or s["collection_status"] == "bounded_success"
                for s in scopes
            )
            else "complete"
            if scopes and all(s["coverage_status"] == "verified_full_inventory" for s in scopes)
            else "unknown"
        )
        if monitoring_status == "completed" and coverage != "complete":
            monitoring_status = "partial" if coverage == "incomplete" else "coverage_unknown"
        return {
            **campaign.model_dump(mode="json"),
            "interval_seconds": owner.interval_seconds,
            "monitoring": {
                "status": monitoring_status,
                "coverage_status": coverage,
                "sources": scopes,
                "last_error": error,
                "last_run_at": (
                    last_operation.started_at.isoformat()
                    if last_operation and last_operation.started_at
                    else None
                ),
                "last_operation_status": last_operation.status if last_operation else None,
                "next_run_at": (
                    (
                        owner.last_scheduled_at + timedelta(seconds=owner.interval_seconds)
                    ).isoformat()
                    if campaign.active and owner.last_scheduled_at
                    else None
                ),
                "scheduler_enabled": bool(
                    config.runtime.operator_worker_enabled and not config.runtime.manual_only
                ),
                "scheduler_running": bool(
                    runtime
                    and runtime.config.runtime.operator_worker_enabled
                    and not runtime.config.runtime.manual_only
                    and runtime.thread
                    and runtime.thread.is_alive()
                ),
                "last_scheduled_at": owner.last_scheduled_at.isoformat()
                if owner.last_scheduled_at
                else None,
                "pending": pending,
                "failed": failed,
            },
        }

    def campaign_list(session: Session, user_id: UUID) -> dict[str, Any]:
        owners = session.scalars(
            select(AccountCampaignRow)
            .join(CampaignRow)
            .where(AccountCampaignRow.user_id == user_id, CampaignRow.deleted_at.is_(None))
            .order_by(AccountCampaignRow.campaign_id)
        )
        return {"items": [campaign_view(session, row) for row in owners]}

    @app.get(prefix + "/campaigns")
    def campaigns(user: CurrentUser, session: Database) -> dict[str, Any]:
        return campaign_list(session, user.id)

    def checked_criteria(
        body: AccountCampaignWrite, session: Session, user: UserRow, *, check_sources: bool = True
    ) -> CampaignCriteria:
        criteria = body.criteria
        if not criteria.source_ids:
            raise HTTPException(422, "Select at least one website")
        if check_sources:
            for key in criteria.source_ids:
                available_source(session, key, user)
        notifications = criteria.notifications.model_dump()
        if notifications["channel"] == "email":
            notifications["email"] = user.email
        elif notifications.get("enabled") and not notifications.get("chat_id"):
            raise HTTPException(422, "Enter your Telegram chat ID")
        semantic = bool(criteria.description.strip() or criteria.custom_notes.strip())
        return CampaignCriteria.model_validate(
            {
                **criteria.model_dump(),
                "automatic_matching": True,
                "automatic_mode": "semantic" if semantic else criteria.automatic_mode,
                "automatic_ai_authorized": semantic or criteria.automatic_ai_authorized,
                "ai_budget_usd": criteria.ai_budget_usd
                if "ai_budget_usd" in criteria.model_fields_set
                else None,
                "ai_request_limit": criteria.ai_request_limit
                if "ai_request_limit" in criteria.model_fields_set
                else None,
                "notifications": notifications,
            }
        )

    @app.post(prefix + "/campaigns", status_code=201)
    def create_campaign(
        body: AccountCampaignWrite, user: CurrentUser, session: Database
    ) -> dict[str, Any]:
        criteria = checked_criteria(body, session, user)
        key = uuid4()
        campaign_service(service(session)).create(key, uuid4(), criteria, active=body.active)
        owner = AccountCampaignRow(
            campaign_id=key, user_id=user.id, interval_seconds=body.interval_seconds
        )
        session.add(owner)
        session.flush()
        return campaign_view(session, owner)

    @app.put(prefix + "/campaigns/{key}")
    def update_campaign(
        key: UUID, body: AccountCampaignWrite, user: CurrentUser, session: Database
    ) -> dict[str, Any]:
        owner = campaign_owner(session, key, user)
        svc = service(session)
        updater = campaign_service(svc)
        current = svc.campaign(key)
        if body.expected_version_id != current.current_criteria_version_id:
            raise HTTPException(409, "Reload campaign before editing")
        criteria = checked_criteria(body, session, user, check_sources=False)
        # Stopping an existing campaign must remain possible during a source outage.
        if body.active or criteria.source_ids != current.criteria.source_ids:
            for source_id in criteria.source_ids:
                available_source(session, source_id, user)
        if criteria != current.criteria:
            updater.update(key, uuid4(), criteria, expected_version_id=body.expected_version_id)
        updater.activate(key, body.active)
        owner.interval_seconds = body.interval_seconds
        return campaign_view(session, owner)

    @app.delete(prefix + "/campaigns/{key}", status_code=204)
    def delete_campaign(key: UUID, user: CurrentUser, session: Database) -> None:
        campaign_owner(session, key, user)
        row = session.get(CampaignRow, key)
        if row is None:
            raise HTTPException(404)
        row.active, row.deleted_at = False, SystemClock().now()

    def tender_statement(user_id: UUID) -> Select[TenderRow, MatchResultRow]:
        # Keep the latest completed analysis visible while a newer version is pending.
        latest = (
            select(
                MatchResultRow.id,
                func.row_number()
                .over(
                    partition_by=(MatchResultRow.campaign_id, MatchResultRow.tender_id),
                    order_by=(
                        (MatchSelectionRow.job_id == MatchResultRow.job_id).desc().nulls_last(),
                        (
                            MatchResultRow.criteria_version_id
                            == CampaignRow.current_criteria_version_id
                        ).desc(),
                        (MatchResultRow.tender_version_id == TenderRow.current_version_id).desc(),
                        MatchResultRow.created_at.desc(),
                        MatchResultRow.id,
                    ),
                )
                .label("position"),
            )
            .join(AccountCampaignRow, AccountCampaignRow.campaign_id == MatchResultRow.campaign_id)
            .join(CampaignRow, CampaignRow.id == MatchResultRow.campaign_id)
            .join(TenderRow, TenderRow.id == MatchResultRow.tender_id)
            .outerjoin(
                MatchSelectionRow,
                (MatchSelectionRow.campaign_id == MatchResultRow.campaign_id)
                & (MatchSelectionRow.tender_id == MatchResultRow.tender_id),
            )
            .where(AccountCampaignRow.user_id == user_id)
            .subquery()
        )
        return (
            select(TenderRow, MatchResultRow)
            .join(MatchResultRow, MatchResultRow.tender_id == TenderRow.id)
            .join(latest, (latest.c.id == MatchResultRow.id) & (latest.c.position == 1))
            .join(CampaignRow, CampaignRow.id == MatchResultRow.campaign_id)
            .where(CampaignRow.deleted_at.is_(None))
        )

    def tender_view(session: Session, tender: TenderRow, result: MatchResultRow) -> dict[str, Any]:
        outcome = result.outcome
        campaign = service(session).campaign(result.campaign_id)
        version = session.get(CriteriaRow, result.criteria_version_id)
        operational = {
            "name",
            "notifications",
            "automatic_matching",
            "automatic_ai_authorized",
            "ai_request_limit",
            "ai_budget_usd",
        }
        previous = CampaignCriteria.model_validate(version.criteria) if version else None
        stale = (
            result.tender_version_id != tender.current_version_id
            or previous is None
            or previous.model_dump(exclude=operational)
            != campaign.criteria.model_dump(exclude=operational)
        )
        return {
            **service(session).tender_view(tender.id).model_dump(mode="json"),
            "campaign_id": str(result.campaign_id),
            "campaign_name": campaign.criteria.name,
            "analysis_stale": stale,
            "analysis_criteria_version_id": str(result.criteria_version_id),
            "relevance": MatchResult.model_validate(outcome).relevance or "NEEDS_REVIEW",
            "reason": "; ".join(
                str(r.get("detail", ""))
                for r in cast(list[dict[str, Any]], outcome.get("reasons", []))
            ),
        }

    @app.get(prefix + "/tenders")
    def tenders(
        user: CurrentUser,
        session: Database,
        q: str = Query(default="", max_length=200),
        source: str = "",
        status: str = "",
        relevance: str = "",
        published_from: datetime | None = None,
        published_to: datetime | None = None,
        deadline_from: datetime | None = None,
        deadline_to: datetime | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        statement = tender_statement(user.id)
        if q:
            statement = statement.where(TenderRow.title.ilike(f"%{q}%"))
        for value, column in ((source, TenderRow.source_id), (status, TenderRow.status)):
            if value:
                statement = statement.where(column == value)
        for date_value, date_column, lower in (
            (published_from, TenderRow.published_at, True),
            (published_to, TenderRow.published_at, False),
            (deadline_from, TenderRow.deadline, True),
            (deadline_to, TenderRow.deadline, False),
        ):
            if date_value:
                if date_value.tzinfo is None:
                    date_value = date_value.replace(tzinfo=UTC)
                statement = statement.where(
                    date_column >= date_value if lower else date_column <= date_value
                )
        if relevance:
            finalized = (
                (MatchResultRow.outcome["result"].as_string() != "unsure")
                & (
                    func.coalesce(
                        MatchResultRow.outcome["evidence_verification"]["status"].as_string(), ""
                    )
                    != "invalid"
                )
                & ~func.jsonb_path_exists(
                    MatchResultRow.outcome,
                    sql_cast("$.model_calls[*] ? (@.error_code != null)", JSONPATH),
                )
            )
            if relevance == "NEEDS_REVIEW":
                statement = statement.where(~finalized)
            elif relevance in {"RELEVANT", "NOT_RELEVANT"}:
                statement = statement.where(
                    finalized,
                    MatchResultRow.outcome["result"].as_string()
                    == ("match" if relevance == "RELEVANT" else "no_match"),
                )
            else:
                raise HTTPException(422, "Unknown relevance filter")
        total = session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = session.execute(
            statement.order_by(TenderRow.last_seen_at.desc(), TenderRow.id)
            .offset(offset)
            .limit(limit)
        )
        return {
            "items": [tender_view(session, t, r) for t, r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @app.get(prefix + "/tenders/{key}")
    def tender(
        key: UUID, user: CurrentUser, session: Database, campaign_id: UUID | None = None
    ) -> dict[str, Any]:
        statement = tender_statement(user.id).where(TenderRow.id == key)
        if campaign_id is not None:
            statement = statement.where(MatchResultRow.campaign_id == campaign_id)
        rows = session.execute(statement.limit(2)).all()
        if not rows:
            raise HTTPException(404)
        if len(rows) > 1:
            raise HTTPException(409, "Select a campaign to view this tender's analysis")
        item, result = rows[0]
        docs = session.scalars(select(TenderDocumentRow).where(TenderDocumentRow.tender_id == key))
        return {
            **tender_view(session, item, result),
            "analysis": result.outcome,
            "documents": [{"url": d.url, "status": d.status, "error": d.error} for d in docs],
        }

    @app.get(prefix + "/notifications")
    def notifications(user: CurrentUser, session: Database) -> dict[str, Any]:
        rows = session.scalars(
            select(TelegramNotificationRow)
            .join(
                AccountCampaignRow,
                AccountCampaignRow.campaign_id == TelegramNotificationRow.campaign_id,
            )
            .where(AccountCampaignRow.user_id == user.id)
            .order_by(TelegramNotificationRow.available_at.desc())
            .limit(100)
        )
        return {
            "items": [
                {
                    "id": str(r.id),
                    "campaign_id": str(r.campaign_id),
                    "status": r.status,
                    "message": r.message,
                    "last_error": r.last_error,
                    "delivered_at": r.delivered_at,
                }
                for r in rows
            ]
        }

    def require_admin(user: UserRow) -> None:
        if user.role != "admin":
            raise HTTPException(403)

    @app.get("/api/v1/admin/users")
    def users(
        user: CurrentUser,
        session: Database,
        q: str = Query(default="", max_length=200),
        active: bool | None = None,
        role: Literal["user", "admin"] | None = None,
    ) -> dict[str, Any]:
        require_admin(user)
        statement = select(UserRow)
        if q:
            statement = statement.where(UserRow.email.ilike(f"%{q}%"))
        if active is not None:
            statement = statement.where(UserRow.active == active)
        if role:
            statement = statement.where(UserRow.role == role)
        return {
            "items": [
                user_view(r) for r in session.scalars(statement.order_by(UserRow.email).limit(1000))
            ]
        }

    @app.patch("/api/v1/admin/users/{key}", response_model=UserView)
    def update_user(key: UUID, body: UserUpdate, user: CurrentUser, session: Database) -> UserView:
        require_admin(user)
        session.execute(text("select pg_advisory_xact_lock(5189062794521009)"))
        target = session.get(UserRow, key)
        if target is None:
            raise HTTPException(404)
        if (
            target.role == "admin"
            and target.active
            and (body.role == "user" or body.active is False)
        ):
            count = session.scalar(
                select(func.count())
                .select_from(UserRow)
                .where(UserRow.role == "admin", UserRow.active.is_(True))
            )
            if count == 1:
                raise HTTPException(409, "Keep at least one active administrator")
        if body.active is not None:
            target.active = body.active
        if body.role is not None:
            target.role = body.role
        session.execute(delete(UserSessionRow).where(UserSessionRow.user_id == key))
        return user_view(target)

    @app.get("/api/v1/admin/users/{key}/campaigns")
    def user_campaigns(key: UUID, user: CurrentUser, session: Database) -> dict[str, Any]:
        require_admin(user)
        return campaign_list(session, key)

    @app.get("/api/v1/admin/users/{key}/tenders")
    def user_tenders(key: UUID, user: CurrentUser, session: Database) -> dict[str, Any]:
        require_admin(user)
        rows = list(session.execute(tender_statement(key).limit(100)))
        return {
            "items": [tender_view(session, t, r) for t, r in rows],
            "total": len(rows),
            "offset": 0,
            "limit": 100,
        }
