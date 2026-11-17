"""Explicit, pinned matching operations for the single operator web interface."""

from typing import TYPE_CHECKING, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy.orm import Session

from tender_system.core.configuration import CampaignOverride, Configuration
from tender_system.matching.matching import (
    manual_matching_settings,
)
from tender_system.operator.operations import OperationRestriction, effective_configuration
from tender_system.persistence.storage import CampaignRow

if TYPE_CHECKING:
    from tender_system.operator.operator_runtime import OperatorRuntime


def bounded_config(
    config: Configuration,
    campaign: UUID,
    semantic: bool,
    evaluation_id: UUID | None = None,
    *,
    session: Session | None = None,
) -> Configuration:
    if semantic and not config.runtime.ai_semantic_enabled:
        raise OperationRestriction("Enable QUARRY_AI_SEMANTIC_ENABLED on the server first")
    settings = config.for_campaign(str(campaign))
    if session is not None:
        from tender_system.operator.ai_settings import assignment

        settings = settings.model_copy(
            update={"model": assignment(session, "tender_matching", settings.model)}
        )
    if semantic and (settings.model.model == "unconfigured" or settings.budget_limit <= 0):
        raise OperationRestriction(
            "Configure a semantic model and a positive provider budget on the server"
        )
    if (
        semantic
        and settings.model.provider not in {"groq", "openrouter", "gemma"}
        and not settings.model.provider.startswith("registry/")
    ):
        raise OperationRestriction("Configure a supported semantic provider on the server")
    settings = manual_matching_settings(settings, semantic, evaluation_id)
    return config.model_copy(
        update={
            "campaigns": {
                **config.campaigns,
                str(campaign): CampaignOverride.model_validate(settings.model_dump()),
            }
        }
    )


def validate_matching_execution(
    session: Session, base: Configuration, payload: dict[str, JsonValue]
) -> Configuration:
    campaign_id = UUID(str(payload["campaign_id"]))
    campaign = session.get(CampaignRow, campaign_id)
    if campaign is None or not campaign.active or campaign.deleted_at is not None:
        raise OperationRestriction("Campaign is inactive or unavailable; preview matching again")
    if str(campaign.current_criteria_version_id) != payload["criteria_version"]:
        raise OperationRestriction("Campaign changed; preview matching again")
    config = bounded_config(
        effective_configuration(session, base),
        campaign_id,
        payload["mode"] == "semantic",
        UUID(str(payload["evaluation_id"])) if payload.get("evaluation_id") else None,
        session=session,
    )
    if (
        config.model_dump(mode="json", by_alias=True, exclude_unset=True)
        != payload["configuration"]
    ):
        raise OperationRestriction("Server matching configuration changed; preview again")
    return config


def execute_matching(
    runtime: "OperatorRuntime", payload: dict[str, JsonValue]
) -> tuple[dict[str, JsonValue], str]:
    with runtime.sessions() as session:
        validate_matching_execution(session, runtime.config, payload)
    from tender_system.operator.automatic_matching import run_jobs

    jobs = tuple(UUID(str(key)) for key in cast(list[JsonValue], payload["job_ids"]))
    result = run_jobs(runtime, UUID(str(payload["operation_id"])), jobs, automatic=False)
    # Include reused evaluations; delivery deduplication prevents repeat messages.
    from tender_system.operator.telegram import enqueue_notification
    from tender_system.persistence.matching_storage import SqlMatchJobRepository

    with runtime.sessions.begin() as session:
        for match in SqlMatchJobRepository(session).results(job_ids=jobs):
            enqueue_notification(session, match, runtime.clock.now())
    return result
