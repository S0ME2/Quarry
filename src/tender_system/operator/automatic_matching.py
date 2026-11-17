"""Consume publication outbox using the existing pinned matching worker."""

from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select

from tender_system.core.configuration import CampaignOverride, CampaignSettings, ModelSettings
from tender_system.core.domain import JobLease, MatchJob, MatchResult
from tender_system.core.ports import MatcherFactory, ModelClient
from tender_system.core.registry import Registry
from tender_system.matching.intelligent_matching import IntelligentMatcher
from tender_system.matching.matching import (
    BatchMatchingWorker,
    MatchingPlanner,
    RulesMatcher,
    manual_matching_settings,
)
from tender_system.operator.matching_budget import BudgetedClient
from tender_system.persistence.matching_storage import (
    SqlCampaignRepository,
    SqlMatchJobRepository,
    StaleMatchLease,
    TransactionalMatchingStore,
    catalog_lock,
)
from tender_system.persistence.storage import (
    MatchingSpendRow,
    MatchJobRow,
    OperationRow,
    SqlTenderRepository,
)

if TYPE_CHECKING:
    from tender_system.operator.operator_runtime import OperatorRuntime


def execute_automatic(
    runtime: "OperatorRuntime", key: UUID, payload: dict[str, JsonValue]
) -> tuple[dict[str, JsonValue], str]:
    from tender_system.collection.documents import DocumentProcessor
    from tender_system.operator.operations import effective_configuration

    with runtime.sessions() as session:
        parent = session.get(OperationRow, UUID(str(payload["source_run_id"])))
        retry_documents = (
            parent is None
            or parent.status != "completed"
            or any(
                parent.result.get(flag) is True
                for flag in ("recovered_after_commit", "document_processing_failed")
            )
        )
    if retry_documents:
        DocumentProcessor(runtime.sessions).process(
            tuple(UUID(value) for value in cast(list[str], payload["tender_ids"])),
            run_id=UUID(str(payload["source_run_id"])),
            document_urls=cast(dict[str, list[str]] | None, payload.get("document_urls")),
        )
    if "job_ids" in payload:
        return run_jobs(
            runtime,
            key,
            tuple(UUID(str(value)) for value in cast(list[str], payload["job_ids"])),
            automatic=True,
        )
    jobs: list[MatchJob] = []
    skipped = 0
    with runtime.sessions.begin() as session:
        catalog_lock(session)
        config = effective_configuration(session, runtime.config)
        campaigns = SqlCampaignRepository(session)
        repository = SqlTenderRepository(session)
        for campaign_key in cast(list[str], payload["campaign_ids"]):
            campaign = campaigns.get(UUID(campaign_key))
            from tender_system.persistence.storage import account_campaign_allowed

            if (
                campaign is None
                or not campaign.active
                or not account_campaign_allowed(
                    session, campaign.id, str(parent.source_id) if parent else ""
                )
            ):
                skipped += 1
                continue
            criteria = campaigns.criteria(campaign.current_criteria_version_id).criteria
            if not criteria.automatic_matching or not config.runtime.matching_enabled:
                skipped += 1
                continue
            semantic = criteria.automatic_mode == "semantic"
            settings = manual_matching_settings(config.for_campaign(campaign_key), semantic)
            from tender_system.operator.ai_settings import assignment

            settings = settings.model_copy(
                update={"model": assignment(session, "tender_matching", settings.model)}
            )
            # Same settings and fingerprints as manual execution; automatic flags never
            # alter matching decisions. Recheck authorization immediately before spending.
            campaign_config = config.model_copy(
                update={
                    "campaigns": {
                        **config.campaigns,
                        campaign_key: CampaignOverride.model_validate(settings.model_dump()),
                    }
                }
            )
            planner = MatchingPlanner(
                campaigns, SqlMatchJobRepository(session), repository, campaign_config
            )
            pairs = []
            for tender_key in cast(list[str], payload["tender_ids"]):
                tender = repository.get(UUID(tender_key))
                if (
                    tender is not None
                    and (not criteria.source_ids or tender.source_id in criteria.source_ids)
                    and planner.validity.evaluate(
                        tender,
                        config.for_matching_source(tender.source_id, campaign_key),
                        runtime.clock.now(),
                    ).matchable
                ):
                    pairs.append((tender.internal_id, campaign.id))
                else:
                    skipped += 1
            jobs.extend(planner.for_pairs(tuple(pairs), runtime.clock.now()))
        operation = session.get(OperationRow, key)
        assert operation is not None
        operation.payload = {
            **operation.payload,
            "job_ids": [str(j.id) for j in jobs],
            "reused_job_ids": [
                str(value)
                for value in session.scalars(
                    select(MatchJobRow.id).where(
                        MatchJobRow.id.in_([job.id for job in jobs]), MatchJobRow.status == "done"
                    )
                )
            ],
        }
    return run_jobs(runtime, key, tuple(j.id for j in jobs), automatic=True, skipped=skipped)


def run_jobs(
    runtime: "OperatorRuntime",
    key: UUID,
    jobs: tuple[UUID, ...],
    *,
    automatic: bool,
    skipped: int = 0,
) -> tuple[dict[str, JsonValue], str]:
    from tender_system.operator.operations import effective_configuration

    with runtime.sessions() as session:
        config = effective_configuration(session, runtime.config)
    # Continuations and restart recovery share spending limits. Only explicit
    # administrator resume grants a new run; lifetime reservations remain intact.
    with runtime.sessions.begin() as session:
        operation = session.get(OperationRow, key)
        if operation is not None:
            run_id = UUID(str(operation.payload.get("matching_run_id") or uuid4()))
            operation.payload = {**operation.payload, "matching_run_id": str(run_id)}
            if operation.result.get("recovered_after_disconnect"):
                for abandoned in session.scalars(
                    select(MatchJobRow)
                    .where(
                        MatchJobRow.id.in_(jobs),
                        MatchJobRow.status == "running",
                        MatchJobRow.lease_expires_at <= runtime.clock.now(),
                    )
                    .with_for_update()
                ):
                    if session.get(MatchingSpendRow, abandoned.id) is not None:
                        abandoned.status = "failed"
                        abandoned.last_error_code = "uncertain_paid_request"
                        abandoned.finished_at = runtime.clock.now()
                    else:
                        abandoned.status, abandoned.last_error_code = (
                            "pending",
                            "worker_interrupted",
                        )
                        abandoned.attempts = max(0, abandoned.attempts - 1)
                        abandoned.available_at, abandoned.finished_at = runtime.clock.now(), None
                    abandoned.lease_token = abandoned.lease_expires_at = None
        else:
            run_id = uuid4()
    totals = dict(claimed=0, completed=0, retried=0, failed=0, skipped=skipped, lost_leases=0)
    stopped = None
    with runtime.sessions() as session:
        pending_ids = tuple(
            session.scalars(
                select(MatchJobRow.id)
                .where(
                    MatchJobRow.id.in_(jobs),
                    (
                        (MatchJobRow.status == "pending")
                        & (MatchJobRow.available_at <= runtime.clock.now())
                    )
                    | (
                        (MatchJobRow.status == "running")
                        & (MatchJobRow.lease_expires_at <= runtime.clock.now())
                    ),
                )
                .order_by(MatchJobRow.created_at, MatchJobRow.id)
                .limit(100 if automatic else 500)
            )
        )
    for job_id in pending_ids:
        with runtime.sessions() as session:
            job = SqlMatchJobRepository(session).get(job_id)
        if job is None:
            continue
        if automatic:
            from tender_system.persistence.storage import account_campaign_allowed

            with runtime.sessions() as session:
                tender = SqlTenderRepository(session).get(job.tender_id)
                if tender is None or not account_campaign_allowed(
                    session, job.campaign_id, tender.source_id
                ):
                    totals["skipped"] += 1
                    continue

        clients: list[BudgetedClient] = []

        def intelligent(
            settings: CampaignSettings,
            pinned_job: MatchJob | None = job,
            attempt_clients: list[BudgetedClient] = clients,
        ) -> IntelligentMatcher:
            assert pinned_job is not None

            def provider(model: ModelSettings) -> ModelClient:
                client = BudgetedClient(
                    lambda: runtime.model_for_settings(model),
                    runtime.sessions,
                    pinned_job.id,
                    pinned_job.campaign_id,
                    run_id,
                    model.provider,
                    automatic,
                    config.runtime.ai_semantic_enabled,
                )
                attempt_clients.append(client)
                return client

            return IntelligentMatcher(settings, runtime.clock, provider)

        matchers: Registry[MatcherFactory] = Registry()
        matchers.register("rules", lambda settings: RulesMatcher(settings, runtime.clock))
        matchers.register("intelligent", intelligent)

        # Budget denial is not a matching result. Keep the original job pending.
        class BudgetStore(TransactionalMatchingStore):
            def complete(
                self,
                lease: JobLease,
                outcome: MatchResult,
                now: datetime,
                *,
                attempt_clients: list[BudgetedClient] = clients,
            ) -> bool:
                if any(c.stopped for c in attempt_clients):
                    with self.sessions.begin() as session:
                        repo = SqlMatchJobRepository(session)
                        try:
                            row = repo._owned(lease, now)
                        except StaleMatchLease:
                            return False
                        row.attempts -= 1
                        repo._finish(row, "pending", now, "matching_budget_stopped")
                    return True
                return super().complete(lease, outcome, now)

        report = BatchMatchingWorker(
            BudgetStore(runtime.sessions, config, job_ids=(job_id,), notify=automatic),
            matchers,
            runtime.clock,
        ).run_selection(1)
        for name, value in report.model_dump().items():
            totals[name] += value
        job_stopped = next((c.stopped for c in clients if c.stopped), None)
        if job_stopped:
            totals["completed"] -= report.completed
            stopped = stopped or job_stopped
        with runtime.sessions.begin() as session:
            operation = session.get(OperationRow, key)
            if operation is not None:
                operation.result = {
                    **operation.result,
                    **totals,
                    "message": "Matching in progress; completed results are saved.",
                }
    with runtime.sessions() as session:
        rows = SqlMatchJobRepository(session).results(job_ids=jobs)
        pending = (
            session.scalar(
                select(func.count())
                .select_from(MatchJobRow)
                .where(MatchJobRow.id.in_(jobs), MatchJobRow.status.in_(("pending", "running")))
            )
            or 0
        )
        next_lease = session.scalar(
            select(func.min(MatchJobRow.lease_expires_at)).where(
                MatchJobRow.id.in_(jobs),
                MatchJobRow.status == "running",
                MatchJobRow.lease_expires_at > runtime.clock.now(),
            )
        )
        failed_jobs = (
            session.scalar(
                select(func.count())
                .select_from(MatchJobRow)
                .where(MatchJobRow.id.in_(jobs), MatchJobRow.status == "failed")
            )
            or 0
        )
        spend = session.scalar(
            select(func.coalesce(func.sum(MatchingSpendRow.accounted_usd), 0)).where(
                MatchingSpendRow.run_id == run_id
            )
        )
        unpriced = (
            session.scalar(
                select(func.count())
                .select_from(MatchingSpendRow)
                .where(MatchingSpendRow.run_id == run_id, MatchingSpendRow.state == "unpriced")
            )
            or 0
        )
        overrun = session.scalar(
            select(MatchingSpendRow.job_id)
            .where(MatchingSpendRow.run_id == run_id, MatchingSpendRow.state == "overrun")
            .limit(1)
        )
        errors = sum(any(c.error_code for c in r.outcome.model_calls) for r in rows)
    if overrun:
        stopped = (
            "Provider usage exceeded its reservation. Further AI is paused; "
            "inspect pricing and usage. Completed results are preserved."
        )
    failed = bool(
        stopped
        or errors
        or failed_jobs
        or totals["failed"]
        or totals["retried"]
        or totals["lost_leases"]
    )
    continuation = bool(
        automatic
        and pending
        and not failed
        and (next_lease or (len(pending_ids) == 100 and totals["completed"] == len(pending_ids)))
    )
    failed = failed or bool(pending and not continuation)
    return {
        **totals,
        "pending": pending,
        "next_attempt_at": next_lease.timestamp()
        if continuation and not pending_ids and next_lease
        else None,
        "technical_errors": errors,
        "estimated_cost_usd": None if unpriced else float(spend or 0),
        "unpriced_requests": unpriced,
        "reused": max(0, len(rows) - totals["completed"]),
        "message": stopped
        or (
            "Remaining evaluations are queued automatically; active leases are honored."
            if continuation
            else "Matching has failures; completed results preserved. Inspect Matching; "
            "forced manual evaluation can retry uncertain paid failures."
            if failed
            else "Matching completed. Equivalent evaluations reused; "
            "disabled campaigns and invalid tenders skipped."
        ),
        "error_code": "matching_budget_stopped"
        if stopped
        else "matching_failed"
        if failed
        else None,
    }, "queued" if continuation else "failed" if failed else "completed"
