"""Single operator HTTP workflows; existing services own execution and persistence."""

import json
from collections.abc import Callable, Iterator
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from pydantic import Field, JsonValue, ValidationInfo, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on

from tender_system.core.clock import SystemClock
from tender_system.core.domain import (
    Contract,
    ExecutionLimits,
    MatchResult,
    RecipeRevision,
    SourceRecipe,
    StoredMatchResult,
)
from tender_system.matching.matching import CampaignService, MatchingPlanner
from tender_system.operator.operations import (
    OperationRestriction,
    OperatorService,
    document,
    effective_configuration,
    operation_view,
    recover_operations,
)
from tender_system.operator.operator_contracts import ManualTender, SourceURL, SourceWrite
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.operator.product_contracts import ActiveWrite, CampaignWrite, Page, TenderView
from tender_system.operator.recipe_input import recipe_url, validate_recipe_content
from tender_system.operator.web_matching import bounded_config, validate_matching_execution
from tender_system.persistence.matching_storage import (
    SqlCampaignRepository,
    SqlMatchJobRepository,
    catalog_lock,
    matching_planner,
)
from tender_system.persistence.storage import (
    ActiveRecipeRow,
    CampaignRow,
    CriteriaRow,
    MatchJobRow,
    MatchResultRow,
    MatchSelectionRow,
    OperationRow,
    OperatorConfigRow,
    RecipeRevisionRow,
    SourceRow,
    SqlTenderRepository,
    TenderRow,
)


class Bounds(Contract):
    max_tenders: int = Field(default=10, ge=1, le=100)
    max_requests: int = Field(default=300, ge=1, le=10000)
    max_duration_seconds: float = Field(default=120, ge=1, le=900)
    max_response_bytes: int = Field(default=10_000_000, ge=1000, le=100_000_000)
    max_total_response_bytes: int = Field(
        default=100_000_000, ge=1000, le=500_000_000, validate_default=True
    )

    @field_validator("max_total_response_bytes")
    @classmethod
    def valid_download_limits(cls, value: int, info: ValidationInfo) -> int:
        if value < info.data.get("max_response_bytes", 0):
            raise ValueError("Total download limit must be at least the per-response limit")
        return value

    def limits(self) -> ExecutionLimits:
        return ExecutionLimits.model_validate(self.model_dump())


class RecipeInput(Bounds):
    # Three independently checked records validate extraction without a collection run.
    max_tenders: int = Field(default=3, ge=1, le=100)
    text: str = Field(min_length=1, max_length=2_000_000)


class MatchingSelection(Contract):
    campaign_id: UUID
    tender_ids: tuple[UUID, ...] = Field(min_length=1, max_length=500)
    mode: Literal["rules", "semantic"] = "rules"
    force_new: bool = False


class WebMatchResult(StoredMatchResult):
    tender_title: str | None


class MatchingStatusRequest(Contract):
    campaign_id: UUID
    tender_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)


class TenderMatchingStatus(Contract):
    tender_id: UUID
    state: Literal["not_evaluated", "pending", "running", "failed", "current", "stale"]
    result: StoredMatchResult | None = None
    error_code: str | None = None


def register_web(
    app: FastAPI,
    service_dependency: Callable[..., Iterator[OperatorService]],
    runtime: OperatorRuntime | None,
) -> None:
    Operator = Annotated[OperatorService, Depends(service_dependency, scope="function")]
    prefix = "/api/v1/web"

    @app.get(prefix + "/activity")
    def activity(svc: Operator) -> Any:
        recover_operations(svc.session, svc.now)
        return svc.operations()

    def schedule(background: BackgroundTasks, key: UUID) -> None:
        if runtime is None:
            raise OperationRestriction(
                "Operation worker unavailable; restart the configured backend"
            )
        background.add_task(runtime.tick, key)

    def active_recipe(svc: Operator, key: str) -> SourceRecipe | None:
        active = svc.session.get(ActiveRecipeRow, key)
        row = svc.session.get(RecipeRevisionRow, (key, active.version)) if active else None
        return RecipeRevision.model_validate(row.revision).recipe if row else None

    def recipe_origin(svc: Operator, key: str) -> OperationRow | None:
        return svc.session.scalar(
            select(OperationRow)
            .where(
                OperationRow.source_id == key,
                OperationRow.result["manual_recipe_import"].as_boolean().is_(True),
            )
            .order_by(OperationRow.created_at.desc(), OperationRow.id.desc())
            .limit(1)
        )

    @app.get(prefix + "/sources/{key}/workspace")
    def source_workspace(key: str, svc: Operator) -> Any:
        origin = recipe_origin(svc, key)
        operations = svc.session.scalars(
            select(OperationRow)
            .where(OperationRow.source_id == key)
            .order_by(OperationRow.created_at.desc())
            .limit(100)
        )
        return {
            "recipe_import": operation_view(origin) if origin else None,
            "operations": [operation_view(row) for row in operations],
            "operation_timeout_seconds": svc.base.runtime.operation_timeout_seconds,
        }

    @app.get(prefix + "/sources/{key}/recipe")
    def saved_recipe(key: str, svc: Operator, stage: Literal["active", "draft"] = "active") -> Any:
        origin = recipe_origin(svc, key)
        recipe = (
            svc.generated_recipe(origin.id)
            if stage == "draft" and origin
            else active_recipe(svc, key)
        )
        if recipe is None:
            raise OperationRestriction("No saved recipe is available for this source")
        validate_recipe_content(recipe.model_dump(mode="json"))
        return {"text": recipe.model_dump_json(indent=2)}

    @app.get(prefix + "/sources/{key}/configuration")
    def source_configuration(key: str, svc: Operator) -> Any:
        return {"configuration": document(svc.config.for_source(key).model_dump(mode="json"))}

    @app.put(prefix + "/sources/{key}/configuration")
    def save_source_configuration(key: str, body: SourceWrite, svc: Operator) -> Any:
        if key == "manual" or (
            svc.session.get(SourceRow, key) is None
            and svc.session.get(OperatorConfigRow, "web/source/" + key) is None
        ):
            raise OperationRestriction("Add or select a tender website first")
        if body.configuration is None:
            raise OperationRestriction("Supply source configuration fields")
        if svc.session.get(SourceRow, key):
            svc.configure_source(key, body)
        else:
            from tender_system.core.configuration import merge
            from tender_system.persistence.recipe_storage import source_lock

            source_lock(svc.session, key)
            control = svc.control(key)
            control["override"] = cast(
                JsonValue,
                merge(
                    cast(dict[str, object], control.get("override", {})),
                    body.configuration.model_dump(mode="json", exclude_unset=True),
                ),
            )
            svc.write("source/" + key, control)
            svc.config = effective_configuration(svc.session, svc.base)
        origin = recipe_origin(svc, key)
        if origin and origin.status in {"awaiting_approval", "failed"}:
            origin.result = {
                **origin.result,
                "validation_passed": False,
                "validation_invalidated_reason": "Source settings changed; validate again",
            }
        return {"configuration": document(svc.config.for_source(key).model_dump(mode="json"))}

    @app.exception_handler(OperationRestriction)
    async def restriction(request: Any, exc: OperationRestriction) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "operation_restricted",
                    "message": str(exc),
                }
            },
        )

    @app.get(prefix + "/sources")
    def sources(svc: Operator) -> dict[str, Any]:
        items = cast(list[dict[str, JsonValue]], svc.sources()["items"])
        by_id = {str(item["id"]): item for item in items}
        for row in svc.session.scalars(
            select(OperatorConfigRow).where(OperatorConfigRow.key.like("web/source/%"))
        ):
            draft = row.value
            key = str(draft["source_id"])
            if key in by_id:
                by_id[key]["name"] = draft["name"]
                by_id[key]["url"] = draft["url"]
            else:
                by_id[key] = {
                    "id": key,
                    "name": draft["name"],
                    "url": draft["url"],
                    "enabled": svc.control(key).get("enabled", True),
                    "recipe_version": None,
                    "tender_count": 0,
                    "health": "draft",
                }
        return {"items": list(by_id.values())}

    @app.post(prefix + "/sources", status_code=201)
    def add_source(
        body: SourceURL, svc: Operator, background: BackgroundTasks
    ) -> dict[str, JsonValue]:
        key = "source-" + uuid4().hex
        metadata: dict[str, JsonValue] = {
            "source_id": key,
            "name": urlsplit(body.url).hostname or body.url,
            "url": body.url,
        }
        svc.write("web/source/" + key, metadata)
        svc.write("source/" + key, {"enabled": True, "manual_only": True})
        operation = svc.enqueue(
            "recipe_generation",
            key,
            {
                "limits": ExecutionLimits(
                    max_tenders=3,
                    max_requests=100,
                    max_duration_seconds=min(360, svc.base.runtime.operation_timeout_seconds),
                    max_response_bytes=2_000_000,
                    max_total_response_bytes=10_000_000,
                ).model_dump(mode="json")
            },
        )
        schedule(background, operation.id)
        return {**metadata, "operation_id": str(operation.id)}

    @app.post(prefix + "/sources/{key}/generate", status_code=202)
    def generate_recipe(key: str, svc: Operator, background: BackgroundTasks) -> Any:
        from tender_system.persistence.recipe_storage import source_lock

        source_lock(svc.session, key)
        if svc.session.get(OperatorConfigRow, "web/source/" + key) is None or active_recipe(
            svc, key
        ):
            raise OperationRestriction("Select a new website draft for automatic generation")
        if svc.session.scalar(
            select(OperationRow.id)
            .where(
                OperationRow.source_id == key,
                OperationRow.status.in_(("queued", "running")),
            )
            .limit(1)
        ):
            raise OperationRestriction("Wait for this source's current operation to finish")
        operation = svc.enqueue(
            "recipe_generation",
            key,
            {
                "limits": ExecutionLimits(
                    max_tenders=3,
                    max_requests=100,
                    max_duration_seconds=min(360, svc.base.runtime.operation_timeout_seconds),
                    max_response_bytes=2_000_000,
                    max_total_response_bytes=10_000_000,
                ).model_dump(mode="json")
            },
        )
        schedule(background, operation.id)
        return operation

    @app.post(prefix + "/sources/{key}/recipe", status_code=202)
    def import_recipe(
        key: str, body: RecipeInput, svc: Operator, background: BackgroundTasks
    ) -> Any:
        from tender_system.persistence.recipe_storage import source_lock

        source_lock(svc.session, key)
        busy = svc.session.scalar(
            select(OperationRow.id)
            .where(
                OperationRow.source_id == key,
                OperationRow.status.in_(("queued", "running")),
            )
            .limit(1)
        )
        if busy is not None:
            raise OperationRestriction("Wait for this source's current operation to finish")
        draft = svc.session.get(OperatorConfigRow, "web/source/" + key)
        current = active_recipe(svc, key)
        if draft is None and current is None:
            raise OperationRestriction("Add a website URL first")
        url = str(draft.value["url"]) if draft else recipe_url(current)
        try:
            value = json.loads(body.text)
            if not isinstance(value, dict):
                raise OperationRestriction("Recipe must be a JSON object")
            # Identity and immutable revision numbers belong to Quarry, not recipe authors.
            value["source_id"] = key
            value["version"] = "recipe-" + uuid4().hex
            recipe = SourceRecipe.model_validate(value)
        except json.JSONDecodeError as exc:
            raise OperationRestriction(
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from None
        validate_recipe_content(recipe.model_dump(mode="json"))
        unchanged = current is not None and current.model_dump(exclude={"version"}) == (
            recipe.model_dump(exclude={"version"})
        )
        if unchanged:
            assert current is not None
            recipe = current
        origin = recipe_origin(svc, key)
        if origin and origin.status in {"awaiting_approval", "failed"}:
            imported = svc.replace_recipe(origin.id, recipe, url)
        else:
            imported = svc.import_recipe(
                recipe,
                url,
                revalidate_source=key if unchanged else None,
                replace_active_source=current is not None and not unchanged,
            )
        limits = Bounds.model_validate(body.model_dump(exclude={"text"})).limits()
        operation = svc.enqueue(
            "source_setup",
            key,
            {
                "operation_id": str(imported.id),
                "limits": limits.model_copy(
                    update={
                        "max_duration_seconds": min(
                            limits.max_duration_seconds, svc.base.runtime.operation_timeout_seconds
                        )
                    }
                ).model_dump(mode="json"),
            },
        )
        schedule(background, operation.id)
        return operation

    @app.post(prefix + "/sources/{key}/collect", status_code=202)
    def collect(key: str, body: Bounds, svc: Operator, background: BackgroundTasks) -> Any:
        limits = body.limits()
        limits = limits.model_copy(
            update={
                "max_duration_seconds": min(
                    limits.max_duration_seconds, svc.base.runtime.operation_timeout_seconds
                )
            }
        )
        operation = svc.collect(key, limits)
        schedule(background, operation.id)
        return operation

    @app.get(prefix + "/tenders", response_model=Page[TenderView])
    def tenders(
        svc: Operator,
        q: str = Query(default="", max_length=200),
        source: str = "",
        status: str = "",
        buyer: str = "",
        location: str = "",
        currency: str = "",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> Any:
        statement = select(TenderRow.id)
        if q:
            statement = statement.where(
                or_(
                    TenderRow.title.ilike(f"%{q}%"),
                    TenderRow.description.ilike(f"%{q}%"),
                    TenderRow.buyer.ilike(f"%{q}%"),
                )
            )
        if source:
            statement = statement.where(TenderRow.source_id == source)
        if status:
            statement = statement.where(TenderRow.status == status)
        if buyer:
            statement = statement.where(TenderRow.buyer.ilike(f"%{buyer}%"))
        if location:
            statement = statement.where(TenderRow.location.ilike(f"%{location}%"))
        if currency:
            statement = statement.where(TenderRow.currency == currency.upper())
        total = svc.session.scalar(select(func.count()).select_from(statement.subquery()))
        keys = svc.session.scalars(
            statement.order_by(TenderRow.last_seen_at.desc(), TenderRow.id)
            .offset(offset)
            .limit(limit)
        )
        return {
            "items": svc.tender_views(tuple(keys)),
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @app.get(prefix + "/tenders/{key}")
    def tender(key: UUID, svc: Operator) -> Any:
        repository = SqlTenderRepository(svc.session)
        item = repository.get(key)
        if item is None:
            raise HTTPException(404)
        return {
            "tender": svc.tender_view(key),
            "product_items": item.product_items,
            "metadata": document(item.raw_data),
            "history": [version.model_dump(mode="json") for version in repository.history(key)],
            "provenance": "manual" if item.source_id == "manual" else "collected",
        }

    @app.post(prefix + "/tenders", response_model=TenderView, status_code=201)
    def manual(body: ManualTender, svc: Operator) -> Any:
        from tender_system.operator.tender_entry import enter_tender

        return svc.tender_view(enter_tender(svc, body).internal_id)

    def campaign_service(svc: Operator) -> CampaignService:
        catalog_lock(svc.session)
        planner = MatchingPlanner(
            SqlCampaignRepository(svc.session),
            SqlMatchJobRepository(svc.session),
            SqlTenderRepository(svc.session),
            svc.config,
            automatic=False,
        )
        return CampaignService(SqlCampaignRepository(svc.session), planner, SystemClock())

    @app.post(prefix + "/matching/status", response_model=tuple[TenderMatchingStatus, ...])
    def matching_status(body: MatchingStatusRequest, svc: Operator) -> Any:
        """One bounded projection; historical result pagination cannot prove current state."""
        campaign = svc.campaign(body.campaign_id)
        current = func.coalesce(
            and_(
                MatchSelectionRow.criteria_version_id == campaign.current_criteria_version_id,
                MatchSelectionRow.tender_version_id == TenderRow.current_version_id,
            ),
            False,
        )
        rows = svc.session.execute(
            select(MatchJobRow, MatchResultRow, current.label("current"))
            .join(TenderRow, TenderRow.id == MatchJobRow.tender_id)
            .outerjoin(MatchResultRow, MatchResultRow.job_id == MatchJobRow.id)
            .outerjoin(MatchSelectionRow, MatchSelectionRow.job_id == MatchJobRow.id)
            .where(
                MatchJobRow.campaign_id == body.campaign_id,
                MatchJobRow.tender_id.in_(body.tender_ids),
            )
            .ext(distinct_on(MatchJobRow.tender_id))
            .order_by(
                MatchJobRow.tender_id,
                current.desc(),
                MatchJobRow.created_at.desc(),
                MatchJobRow.id.desc(),
            )
        )
        found = {
            job.tender_id: TenderMatchingStatus(
                tender_id=job.tender_id,
                state=(
                    "stale"
                    if not is_current
                    else "current"
                    if result is not None
                    else "failed"
                    if job.status in {"done", "failed"}
                    else job.status
                ),
                result=StoredMatchResult.model_validate(result, from_attributes=True)
                if result is not None
                else None,
                error_code=job.last_error_code,
            )
            for job, result, is_current in rows
        }
        return tuple(
            found.get(key, TenderMatchingStatus(tender_id=key, state="not_evaluated"))
            for key in dict.fromkeys(body.tender_ids)
        )

    from tender_system.operator.matching_budget import MatchingBudget, budget_settings
    from tender_system.persistence.storage import MatchingSpendRow

    @app.get(prefix + "/ai-budget")
    def ai_budget(svc: Operator) -> Any:
        rows = tuple(svc.session.scalars(select(MatchingSpendRow)))
        settings = budget_settings(svc.session)
        model = svc.config.global_settings.campaign.model
        charges = [
            call.cost
            for result in svc.session.scalars(
                select(MatchResultRow).join(
                    MatchingSpendRow, MatchingSpendRow.job_id == MatchResultRow.job_id
                )
            )
            for call in MatchResult.model_validate(result.outcome).model_calls
        ]
        return {
            "settings": settings.model_dump(mode="json"),
            "active_model": {"provider": model.provider, "model": model.model},
            "pricing_matches_model": settings.provider == model.provider
            and settings.model == model.model
            and settings.input_usd_per_million is not None
            and settings.input_usd_per_million > 0
            and settings.output_usd_per_million is not None
            and settings.output_usd_per_million > 0,
            "accounted_cost_usd": float(sum(r.accounted_usd for r in rows)),
            "estimated_cost_usd": (
                None
                if any(r.state == "unpriced" for r in rows)
                else float(sum(r.accounted_usd for r in rows))
            ),
            "provider_reported_cost_usd": (
                float(sum(c for c in charges if c is not None))
                if charges and len(charges) == len(rows) and all(c is not None for c in charges)
                else None
            ),
            "requests": len(rows),
            "uncertain_requests": sum(
                r.state in {"reserved", "uncertain", "unpriced", "overrun"} for r in rows
            ),
        }

    @app.put(prefix + "/ai-budget")
    def save_ai_budget(body: MatchingBudget, svc: Operator) -> Any:
        catalog_lock(svc.session)
        row = svc.session.get(OperatorConfigRow, "matching_budget")
        if row is None:
            row = OperatorConfigRow(key="matching_budget", updated_at=svc.now)
            svc.session.add(row)
        model = svc.config.global_settings.campaign.model
        # Bind omitted pricing identity to the server model once, at save time.
        # A later server model change must not silently reuse another model's rates.
        body = body.model_copy(
            update={
                "provider": body.provider or model.provider,
                "model": body.model or model.model,
            }
        )
        row.value, row.updated_at = body.model_dump(mode="json"), svc.now
        return body

    @app.post(prefix + "/campaigns", status_code=201)
    def create_campaign(body: CampaignWrite, svc: Operator) -> Any:
        key = uuid4()
        campaign_service(svc).create(key, uuid4(), body.criteria, active=body.active)
        return svc.campaign(key)

    @app.post(prefix + "/campaigns/{key}/criteria")
    def update_campaign(key: UUID, body: CampaignWrite, svc: Operator) -> Any:
        svc.campaign(key)
        if body.expected_version_id is None:
            raise OperationRestriction("Reload the campaign before editing its criteria")
        campaign_service(svc).update(
            key, uuid4(), body.criteria, expected_version_id=body.expected_version_id
        )
        return svc.campaign(key)

    @app.patch(prefix + "/campaigns/{key}/state")
    def campaign_state(key: UUID, body: ActiveWrite, svc: Operator) -> Any:
        svc.campaign(key)
        campaign_service(svc).activate(key, body.active)
        return svc.campaign(key)

    @app.post(prefix + "/matching/preview", status_code=201)
    def preview(body: MatchingSelection, svc: Operator) -> Any:
        if not svc.campaign(body.campaign_id).active:
            raise OperationRestriction("Activate the campaign before matching")
        if len(set(body.tender_ids)) != len(body.tender_ids):
            raise OperationRestriction("Select each tender once")
        evaluation_id = uuid4() if body.force_new else None
        config = bounded_config(
            effective_configuration(svc.session, svc.base),
            body.campaign_id,
            body.mode == "semantic",
            evaluation_id,
            session=svc.session,
        )
        settings = config.for_campaign(str(body.campaign_id))
        repo = SqlTenderRepository(svc.session)
        versions: dict[str, JsonValue] = {}
        for key in body.tender_ids:
            tender = repo.get(key)
            if tender is None:
                raise OperationRestriction("Selected tender no longer exists")
            from tender_system.core.tenders import ConfigurableTenderValidity

            validity = ConfigurableTenderValidity().evaluate(
                tender, config.for_matching_source(tender.source_id, str(body.campaign_id)), svc.now
            )
            if not validity.matchable:
                raise OperationRestriction(
                    "Tender cannot be matched: "
                    + "; ".join(reason.detail for reason in validity.reasons)
                )
            versions[str(key)] = str(tender.current_version_id)
        operation = svc.enqueue(
            "web_matching",
            None,
            {
                **body.model_dump(mode="json"),
                "evaluation_id": str(evaluation_id) if evaluation_id else None,
                "configuration": config.model_dump(mode="json", by_alias=True, exclude_unset=True),
                "versions": versions,
                "criteria_version": str(svc.campaign(body.campaign_id).current_criteria_version_id),
            },
        )
        row = svc.session.get(OperationRow, operation.id)
        assert row is not None
        row.status = "awaiting_approval"
        row.result = {
            "mode": body.mode,
            "provider": settings.model.provider if body.mode == "semantic" else None,
            "model": settings.model.model if body.mode == "semantic" else None,
            "selected_count": len(body.tender_ids),
            "maximum_model_requests": len(body.tender_ids) if body.mode == "semantic" else 0,
            "maximum_output_tokens": settings.model.max_output_tokens
            if body.mode == "semantic"
            else 0,
            "input_token_estimate": None,
            "cost_estimate": None,
            "spending_protection": "Global and campaign budgets checked before each request. "
            "Costs are estimates; uncertain attempts retain reservations.",
            "automatic_retries": False,
            "escalation": False,
            "new_evaluation": evaluation_id is not None,
        }
        return operation_view(row)

    @app.post(prefix + "/matching/{key}/confirm", status_code=202)
    def confirm(key: UUID, svc: Operator, background: BackgroundTasks) -> Any:
        catalog_lock(svc.session)
        row = svc.session.scalar(
            select(OperationRow).where(OperationRow.id == key).with_for_update()
        )
        if row is None or row.kind != "web_matching" or row.status != "awaiting_approval":
            raise OperationRestriction(
                "This matching preview has already been submitted or is unavailable"
            )
        campaign = UUID(str(row.payload["campaign_id"]))
        if (
            str(svc.campaign(campaign).current_criteria_version_id)
            != row.payload["criteria_version"]
        ):
            raise OperationRestriction("Campaign changed; preview matching again")
        config = bounded_config(
            effective_configuration(svc.session, svc.base),
            campaign,
            row.payload["mode"] == "semantic",
            UUID(str(row.payload["evaluation_id"])) if row.payload.get("evaluation_id") else None,
            session=svc.session,
        )
        pinned = row.payload["configuration"]
        if config.model_dump(mode="json", by_alias=True, exclude_unset=True) != pinned:
            raise OperationRestriction("Server matching configuration changed; preview again")
        versions = cast(dict[str, str], row.payload["versions"])
        jobs = matching_planner(svc.session, config).for_pairs(
            tuple((UUID(t), campaign) for t in versions), svc.now
        )
        for tender_id, version_id in versions.items():
            tender = SqlTenderRepository(svc.session).get(UUID(tender_id))
            if tender is None or str(tender.current_version_id) != version_id:
                raise OperationRestriction("Selected tender changed; preview again")
        if any(str(job.criteria_version_id) != row.payload["criteria_version"] for job in jobs):
            raise OperationRestriction("Selection changed while planning; preview matching again")
        row.payload = {
            **row.payload,
            "operation_id": str(row.id),
            "job_ids": [str(job.id) for job in jobs],
        }
        row.status = "queued"
        schedule(background, row.id)
        return operation_view(row)

    @app.get(prefix + "/results", response_model=Page[WebMatchResult])
    def results(
        svc: Operator,
        campaign_id: UUID,
        offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=50, ge=1, le=100),
        state: Literal[
            "all", "match", "no_match", "unsure", "technical_error", "unverified_evidence"
        ] = "all",
        view: Literal["history", "current"] = "history",
    ) -> Any:
        rows, total = SqlMatchJobRepository(svc.session).result_page(
            campaign_id, offset, limit, state, view
        )
        titles = dict(
            svc.session.execute(
                select(TenderRow.id, TenderRow.title).where(
                    TenderRow.id.in_([r.tender_id for r in rows])
                )
            ).all()
        )
        return {
            "items": [
                {**r.model_dump(mode="json"), "tender_title": titles.get(r.tender_id)} for r in rows
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @app.get(prefix + "/campaign-options")
    def campaign_options(
        svc: Operator,
        q: str = Query(default="", max_length=200),
        offset: int = Query(default=0, ge=0, le=100000),
    ) -> Any:
        query = (
            select(CampaignRow.id, CriteriaRow.criteria, CriteriaRow.number)
            .join(CriteriaRow, CriteriaRow.id == CampaignRow.current_criteria_version_id)
            .where(CampaignRow.active.is_(True), CampaignRow.deleted_at.is_(None))
        )
        if q:
            query = query.where(CriteriaRow.criteria["name"].as_string().ilike(f"%{q}%"))
        total = svc.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = svc.session.execute(query.order_by(CampaignRow.id).offset(offset).limit(50))
        return {
            "items": [
                {"id": str(key), "name": criteria["name"], "criteria_number": number}
                for key, criteria, number in rows
            ],
            "total": total,
            "offset": offset,
            "limit": 50,
        }

    @app.post(prefix + "/operations/{key}/resume", status_code=202)
    def resume(key: UUID, svc: Operator, background: BackgroundTasks) -> Any:
        row = svc.session.scalar(
            select(OperationRow).where(OperationRow.id == key).with_for_update()
        )
        if (
            row is None
            or row.status
            not in ({"queued", "failed"} if row.kind == "automatic_matching" else {"queued"})
            or row.kind
            not in {
                "collection",
                "recipe_validation",
                "source_setup",
                "activation",
                "web_matching",
                "automatic_matching",
            }
        ):
            raise OperationRestriction(
                "Only an unstarted queued operation can be resumed. "
                "Failed evaluations are never retried automatically."
            )
        if row.kind in {"collection", "recipe_validation", "source_setup"}:
            limits = row.payload.get("limits")
            if not isinstance(limits, dict) or limits.get("max_tenders") is None:
                raise OperationRestriction(
                    "This legacy operation has no bounded scope. "
                    "Start a new bounded operation from Sources."
                )
            Bounds.model_validate(limits)
        if row.kind == "activation":
            origin = svc.operation(UUID(str(row.payload["operation_id"])))
            limits = origin.result.get("execution_limits")
            if not isinstance(limits, dict) or limits.get("max_tenders") is None:
                raise OperationRestriction("Revalidate a bounded sample before web approval")
            Bounds.model_validate(limits)
        if row.kind == "web_matching":
            if not row.payload.get("job_ids"):
                raise OperationRestriction("Review and confirm the matching selection first")
            validate_matching_execution(svc.session, svc.base, row.payload)
        if row.kind == "automatic_matching" and row.status == "failed":
            row.payload = {k: v for k, v in row.payload.items() if k != "matching_run_id"}
        row.status = "queued"
        schedule(background, row.id)
        return operation_view(row)
