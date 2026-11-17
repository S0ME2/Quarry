"""PostgreSQL matching repositories and short transactional worker facade."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4, uuid5

from sqlalchemy import cast as sql_cast
from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import JSONPATH, insert
from sqlalchemy.orm import Session, sessionmaker

from tender_system.core.clock import SystemClock
from tender_system.core.configuration import CampaignSettings, Configuration
from tender_system.core.domain import (
    Campaign,
    CriteriaVersion,
    JobLease,
    MatchingInput,
    MatchJob,
    MatchResult,
    StoredMatchResult,
    Tender,
)
from tender_system.core.ports import Clock, TenderValidityPolicy
from tender_system.core.tenders import (
    ConfigurableTenderValidity,
    IdempotencyConflict,
    reconstruct_history,
)
from tender_system.matching.matching import MatchingPlanner, evaluation_context, settings_digest
from tender_system.persistence.storage import (
    JOB_IDENTITY,
    CampaignRow,
    CriteriaRow,
    MatchJobRow,
    MatchResultRow,
    MatchSelectionRow,
    SqlTenderRepository,
    TenderObservationRow,
    TenderRow,
    account_campaign_allowed,
)

CATALOG_LOCK = 0x54454E44455205


def catalog_lock(session: Session) -> None:
    session.execute(select(func.pg_advisory_xact_lock(CATALOG_LOCK)))


class StaleMatchLease(ValueError):
    pass


class SqlCampaignRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @contextmanager
    def atomic(self) -> Iterator[None]:
        with self.session.begin_nested():
            yield

    def get(self, campaign_id: UUID) -> Campaign | None:
        row = self.session.get(CampaignRow, campaign_id, populate_existing=True)
        return (
            None
            if row is None or row.deleted_at is not None
            else Campaign(
                id=row.id,
                active=row.active,
                current_criteria_version_id=row.current_criteria_version_id,
            )
        )

    def active(self) -> tuple[Campaign, ...]:
        return tuple(
            Campaign(
                id=r.id, active=r.active, current_criteria_version_id=r.current_criteria_version_id
            )
            for r in self.session.scalars(
                select(CampaignRow)
                .where(CampaignRow.active, CampaignRow.deleted_at.is_(None))
                .order_by(CampaignRow.id)
            )
            if account_campaign_allowed(self.session, r.id, "")
        )

    def criteria(self, version_id: UUID) -> CriteriaVersion:
        row = self.session.get(CriteriaRow, version_id, populate_existing=True)
        if row is None:
            raise KeyError("criteria version not found")
        return CriteriaVersion.model_validate(
            {
                "id": row.id,
                "campaign_id": row.campaign_id,
                "criteria": row.criteria,
                "created_at": row.created_at,
                "number": row.number,
                "previous_version_id": row.previous_version_id,
            }
        )

    def store(self, campaign: Campaign, version: CriteriaVersion) -> None:
        with self.session.begin_nested():
            catalog_lock(self.session)
            if (
                version.campaign_id != campaign.id
                or version.id != campaign.current_criteria_version_id
            ):
                raise ValueError("criteria/campaign ownership mismatch")
            prior = self.session.get(CriteriaRow, version.id)
            existing = self.session.get(CampaignRow, campaign.id, populate_existing=True)
            if existing is not None and existing.deleted_at is not None:
                raise ValueError("campaign deleted")
            if prior is not None:
                if self.criteria(version.id) != version:
                    raise IdempotencyConflict("criteria version ID reused")
                return
            row = self.session.scalar(
                select(CampaignRow).where(CampaignRow.id == campaign.id).with_for_update()
            )
            if row is None:
                if version.previous_version_id is not None or version.number != 1:
                    raise ValueError("new campaign needs initial criteria")
                row = CampaignRow(
                    id=campaign.id, active=campaign.active, current_criteria_version_id=version.id
                )
                self.session.add(row)
                self.session.flush()
            elif (
                row.current_criteria_version_id != version.previous_version_id
                or version.number != self.criteria(row.current_criteria_version_id).number + 1
            ):
                raise ValueError("criteria baseline changed")
            self.session.add(
                CriteriaRow(
                    **version.model_dump(exclude={"criteria"}),
                    criteria=version.criteria.model_dump(mode="json"),
                )
            )
            row.current_criteria_version_id = version.id
            # Criteria edits do not overwrite concurrently changed activation state.
            self.session.flush()
            self.session.execute(
                update(MatchJobRow)
                .where(
                    MatchJobRow.campaign_id == campaign.id,
                    MatchJobRow.criteria_version_id != version.id,
                    MatchJobRow.status.in_(("pending", "running")),
                )
                .values(
                    status="failed",
                    last_error_code="criteria_superseded",
                    lease_token=None,
                    lease_expires_at=None,
                    finished_at=version.created_at,
                )
            )

    def set_active(self, campaign_id: UUID, active: bool) -> Campaign:
        catalog_lock(self.session)
        row = self.session.scalar(
            select(CampaignRow).where(CampaignRow.id == campaign_id).with_for_update()
        )
        if row is None or row.deleted_at is not None:
            raise KeyError("campaign not found")
        row.active = active
        self.session.flush()
        return Campaign(
            id=row.id,
            active=row.active,
            current_criteria_version_id=row.current_criteria_version_id,
        )

    def delete(self, campaign_id: UUID, now: datetime) -> None:
        """Soft deletion keeps criteria, match results and feedback auditable."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("deletion requires timezone-aware time")
        catalog_lock(self.session)
        row = self.session.get(CampaignRow, campaign_id, populate_existing=True)
        if row is None:
            raise KeyError("campaign not found")
        if row.deleted_at is None:
            row.active, row.deleted_at = False, now
            self.session.flush()


class SqlMatchJobRepository:
    def __init__(self, session: Session, clock: Clock | None = None) -> None:
        self.session, self.clock = session, clock or SystemClock()

    def get(self, job_id: UUID) -> MatchJob | None:
        row = self.session.get(MatchJobRow, job_id, populate_existing=True)
        return None if row is None else self._job(row)

    def _job(self, row: MatchJobRow) -> MatchJob:
        return MatchJob.model_validate({name: getattr(row, name) for name in MatchJob.model_fields})

    def jobs(
        self, *, campaign_id: UUID | None = None, tender_id: UUID | None = None
    ) -> tuple[MatchJob, ...]:
        query = select(MatchJobRow).order_by(MatchJobRow.created_at, MatchJobRow.id)
        if campaign_id is not None:
            query = query.where(MatchJobRow.campaign_id == campaign_id)
        if tender_id is not None:
            query = query.where(MatchJobRow.tender_id == tender_id)
        return tuple(self._job(row) for row in self.session.scalars(query))

    def enqueue(self, jobs: tuple[MatchJob, ...]) -> tuple[MatchJob, ...]:
        with self.session.begin_nested():
            # Same matching inputs may span versions that only changed URLs/documents.
            # Serialize reuse with other planners; external calls never hold this lock.
            catalog_lock(self.session)
            canonical: dict[UUID, MatchJob] = {}
            keys = (
                "tender_id",
                "campaign_id",
                "criteria_version_id",
                "matcher_implementation",
                "matcher_version",
                "settings_hash",
            )
            candidates = tuple(
                job
                for job in jobs
                if job.matcher_implementation in {"rules", "intelligent"}
                and job.matcher_config.get("evaluation_context") is not None
            )
            for offset in range(0, len(candidates), 500):
                batch = candidates[offset : offset + 500]
                rows = self.session.scalars(
                    select(MatchJobRow)
                    .where(
                        tuple_(*(getattr(MatchJobRow, key) for key in keys)).in_(
                            [tuple(getattr(job, key) for key in keys) for job in batch]
                        )
                    )
                    .order_by(MatchJobRow.created_at, MatchJobRow.id)
                )
                prior: dict[tuple[object, ...], MatchJob] = {}
                for row in rows:
                    prior.setdefault(tuple(getattr(row, key) for key in keys), self._job(row))
                for job in batch:
                    if match := prior.get(tuple(getattr(job, key) for key in keys)):
                        canonical[job.id] = job.model_copy(
                            update={"id": match.id, "tender_version_id": match.tender_version_id}
                        )
            selected = tuple(canonical.get(job.id, job) for job in jobs)
            self._enqueue(selected)
            for original, chosen in zip(jobs, selected, strict=True):
                statement = insert(MatchSelectionRow).values(
                    campaign_id=original.campaign_id,
                    tender_id=original.tender_id,
                    job_id=chosen.id,
                    tender_version_id=original.tender_version_id,
                    criteria_version_id=original.criteria_version_id,
                )
                self.session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[MatchSelectionRow.campaign_id, MatchSelectionRow.tender_id],
                        set_={
                            "job_id": statement.excluded.job_id,
                            "tender_version_id": statement.excluded.tender_version_id,
                            "criteria_version_id": statement.excluded.criteria_version_id,
                        },
                    )
                )
            return selected

    def _enqueue(self, jobs: tuple[MatchJob, ...]) -> None:
        values = []
        for job in jobs:
            if job.status != "pending" or job.attempts:
                raise ValueError("enqueue requires a new pending job")
            config = (
                CampaignSettings.model_validate(job.matcher_config)
                if job.matcher_config
                else CampaignSettings(
                    matcher_adapter=job.matcher_implementation, matcher_version=job.matcher_version
                )
            )
            digest = settings_digest(config)
            if job.settings_hash and job.settings_hash != digest:
                raise ValueError("job configuration fingerprint mismatch")
            if (
                job.matcher_implementation != config.matcher_adapter
                or job.matcher_version != config.matcher_version
            ):
                raise ValueError("job matcher differs from configuration")
            values.append(
                {
                    **job.model_dump(),
                    "matcher_config": config.model_dump(mode="json"),
                    "settings_hash": digest,
                    "available_at": job.available_at or job.created_at,
                    "lease_token": None,
                    "lease_expires_at": None,
                }
            )
        for offset in range(0, len(values), 500):
            statement = insert(MatchJobRow).values(values[offset : offset + 500])
            self.session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_match_job_work",
                    set_={
                        "status": "pending",
                        "attempts": 0,
                        "available_at": statement.excluded.available_at,
                        "finished_at": None,
                        "last_error_code": None,
                    },
                    where=(MatchJobRow.status == "failed")
                    & (
                        MatchJobRow.last_error_code.in_(
                            ("tender_invalid", "evaluation_context_changed")
                        )
                    ),
                )
            )

    def claim(self, now: datetime, lease_seconds: float) -> JobLease | None:
        leases = self.claim_batch(now, 1, lease_seconds)
        return leases[0] if leases else None

    def claim_batch(
        self,
        now: datetime,
        limit: int,
        lease_seconds: float | None = None,
        *,
        job_ids: tuple[UUID, ...] | None = None,
    ) -> tuple[JobLease, ...]:
        with self.session.begin_nested():
            return self._claim_batch(now, limit, lease_seconds, job_ids)

    def _claim_batch(
        self,
        now: datetime,
        limit: int,
        lease_seconds: float | None,
        job_ids: tuple[UUID, ...] | None = None,
    ) -> tuple[JobLease, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("claim requires timezone-aware time")
        if limit < 1 or (lease_seconds is not None and lease_seconds <= 0):
            raise ValueError("claim limit/lease must be positive")
        exhausted = self.session.scalars(
            select(MatchJobRow)
            .where(
                MatchJobRow.status == "running",
                *([MatchJobRow.id.in_(job_ids)] if job_ids is not None else []),
                MatchJobRow.lease_expires_at <= now,
                MatchJobRow.attempts >= MatchJobRow.max_attempts,
            )
            .with_for_update(skip_locked=True)
        )
        for row in exhausted:
            self._finish(row, "failed", now, "retry_exhausted")
        rows = tuple(
            self.session.scalars(
                select(MatchJobRow)
                .join(CampaignRow, CampaignRow.id == MatchJobRow.campaign_id)
                .where(
                    CampaignRow.active,
                    *([MatchJobRow.id.in_(job_ids)] if job_ids is not None else []),
                    CampaignRow.current_criteria_version_id == MatchJobRow.criteria_version_id,
                    MatchJobRow.attempts < MatchJobRow.max_attempts,
                    or_(
                        (MatchJobRow.status == "pending") & (MatchJobRow.available_at <= now),
                        (MatchJobRow.status == "running") & (MatchJobRow.lease_expires_at <= now),
                    ),
                )
                .order_by(MatchJobRow.created_at, MatchJobRow.id)
                .limit(limit)
                .with_for_update(of=MatchJobRow, skip_locked=True)
            )
        )
        campaign_counts: dict[UUID, int] = {}
        selected = []
        for row in rows:
            count = campaign_counts.get(row.campaign_id, 0)
            if count < CampaignSettings.model_validate(row.matcher_config).batch_size:
                selected.append(row)
                campaign_counts[row.campaign_id] = count + 1
        rows = tuple(selected)
        budget = (
            sum(CampaignSettings.model_validate(r.matcher_config).timeout_seconds for r in rows) + 1
        )
        leases = []
        for row in rows:
            settings = CampaignSettings.model_validate(row.matcher_config)
            row.status, row.lease_token = "running", uuid4()
            row.attempts += 1
            row.started_at = now
            row.lease_expires_at = now + timedelta(
                seconds=lease_seconds
                if lease_seconds is not None
                else max(settings.lease_seconds, budget)
            )
            leases.append(
                JobLease(job=self._job(row), token=row.lease_token, expires_at=row.lease_expires_at)
            )
        self.session.flush()
        return tuple(leases)

    def _owned(self, lease: JobLease, now: datetime) -> MatchJobRow:
        row = self.session.scalar(
            select(MatchJobRow)
            .where(MatchJobRow.id == lease.job.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.status != "running"
            or row.lease_token != lease.token
            or row.lease_expires_at is None
            or row.lease_expires_at <= now
        ):
            raise StaleMatchLease("lease expired or belongs to another attempt")
        return row

    def _finish(
        self, row: MatchJobRow, status: str, now: datetime, error: str | None = None
    ) -> None:
        row.status, row.last_error_code = status, error
        row.finished_at = now if status in {"done", "failed"} else None
        row.lease_token = row.lease_expires_at = None
        self.session.flush()

    def retry(self, lease: JobLease, available_at: datetime, error_code: str) -> None:
        row = self._owned(lease, self.clock.now())
        row.available_at = available_at
        self._finish(
            row,
            "failed" if row.attempts >= row.max_attempts else "pending",
            self.clock.now(),
            error_code,
        )

    def complete(self, lease: JobLease, result: StoredMatchResult) -> None:
        now = self.clock.now()
        if result.completion_token != lease.token:
            raise StaleMatchLease("completion token differs from lease")
        with self.session.begin_nested():
            prior = self.session.scalar(
                select(MatchResultRow).where(MatchResultRow.job_id == lease.job.id)
            )
            if prior is not None:
                if self._result(prior) != result:
                    raise IdempotencyConflict("completion replay has different result")
                return
            row = self._owned(lease, now)
            if result.job_id != row.id or any(
                getattr(result, key) != getattr(row, key) for key in JOB_IDENTITY
            ):
                raise ValueError("result does not belong to pinned job")
            outcome = result.outcome
            if any(
                getattr(outcome, key) != getattr(row, key)
                for key in (
                    "matcher_implementation",
                    "matcher_version",
                    "tender_version_id",
                    "criteria_version_id",
                )
            ):
                raise ValueError("result outcome version binding mismatch")
            self.session.add(
                MatchResultRow(
                    **result.model_dump(exclude={"outcome"}),
                    outcome=outcome.model_dump(mode="json"),
                )
            )
            self._finish(row, "done", now)

    def _result(self, row: MatchResultRow) -> StoredMatchResult:
        return StoredMatchResult.model_validate(
            {name: getattr(row, name) for name in StoredMatchResult.model_fields}
        )

    def results(
        self,
        *,
        campaign_id: UUID | None = None,
        tender_id: UUID | None = None,
        job_ids: tuple[UUID, ...] | None = None,
    ) -> tuple[StoredMatchResult, ...]:
        query = select(MatchResultRow).order_by(MatchResultRow.created_at, MatchResultRow.id)
        if campaign_id is not None:
            query = query.where(MatchResultRow.campaign_id == campaign_id)
        if tender_id is not None:
            query = query.where(MatchResultRow.tender_id == tender_id)
        if job_ids is not None:
            query = query.where(MatchResultRow.job_id.in_(job_ids))
        return tuple(self._result(row) for row in self.session.scalars(query))

    def result_page(
        self, campaign_id: UUID, offset: int, limit: int, state: str = "all", view: str = "history"
    ) -> tuple[tuple[StoredMatchResult, ...], int]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ValueError("Invalid result page")
        # Operator history includes superseded evaluations and diagnostic outcomes.
        # Current verified matches are selected separately by the tender repository.
        query = select(MatchResultRow).where(MatchResultRow.campaign_id == campaign_id)
        technical = func.jsonb_path_exists(
            MatchResultRow.outcome, sql_cast("$.model_calls[*] ? (@.error_code != null)", JSONPATH)
        )
        invalid = func.coalesce(
            MatchResultRow.outcome["evidence_verification"]["status"].as_string() == "invalid",
            False,
        )
        if view == "current":
            query = (
                query.join(MatchSelectionRow, MatchSelectionRow.job_id == MatchResultRow.job_id)
                .join(CampaignRow, CampaignRow.id == MatchSelectionRow.campaign_id)
                .join(TenderRow, TenderRow.id == MatchSelectionRow.tender_id)
                .where(
                    MatchSelectionRow.campaign_id == campaign_id,
                    MatchSelectionRow.criteria_version_id
                    == CampaignRow.current_criteria_version_id,
                    MatchSelectionRow.tender_version_id == TenderRow.current_version_id,
                    MatchResultRow.outcome["result"].as_string() == "match",
                    ~technical,
                    ~invalid,
                )
            )
        elif view != "history":
            raise ValueError("Invalid result view")
        if state == "technical_error":
            query = query.where(technical)
        elif state == "unverified_evidence":
            query = query.where(~technical, invalid)
        elif state in {"match", "no_match", "unsure"}:
            query = query.where(
                ~technical, ~invalid, MatchResultRow.outcome["result"].as_string() == state
            )
        elif state != "all":
            raise ValueError("Invalid result filter")
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(
            query.order_by(MatchResultRow.created_at.desc(), MatchResultRow.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return tuple(self._result(row) for row in rows), total


def matching_planner(session: Session, config: Configuration) -> MatchingPlanner:
    catalog_lock(session)
    return MatchingPlanner(
        SqlCampaignRepository(session),
        SqlMatchJobRepository(session),
        SqlTenderRepository(session),
        config,
    )


class TransactionalMatchingStore:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        config: Configuration | None = None,
        validity: TenderValidityPolicy | None = None,
        *,
        job_ids: tuple[UUID, ...] | None = None,
        notify: bool = False,
    ) -> None:
        self.notify = notify
        self.job_ids = job_ids
        self.sessions, self.config = sessions, config or Configuration()
        self.validity = validity or ConfigurableTenderValidity()

    def claim(self, limit: int, now: datetime) -> tuple[JobLease, ...]:
        with self.sessions.begin() as session:
            return SqlMatchJobRepository(session).claim_batch(
                now,
                min(limit, self.config.global_settings.campaign.batch_size),
                job_ids=self.job_ids,
            )

    def _eligible(self, session: Session, lease: JobLease, now: datetime) -> MatchingInput | None:
        jobs = SqlMatchJobRepository(session)
        campaign = session.scalar(
            select(CampaignRow).where(CampaignRow.id == lease.job.campaign_id).with_for_update()
        )
        row = jobs._owned(lease, now)
        if campaign is None or campaign.current_criteria_version_id != row.criteria_version_id:
            jobs._finish(row, "failed", now, "criteria_superseded")
            return None
        if not campaign.active:
            row.attempts -= 1  # Administrative deferral does not consume the retry budget.
            row.available_at = now
            jobs._finish(row, "pending", now, "campaign_inactive")
            return None
        repository = SqlTenderRepository(session)
        tender = repository.get(row.tender_id)
        if (
            tender is None
            or not self.validity.evaluate(
                tender,
                self.config.for_matching_source(tender.source_id, str(lease.job.campaign_id)),
                now,
            ).matchable
        ):
            jobs._finish(row, "failed", now, "tender_invalid")
            return None
        versions = repository.history(tender.internal_id)
        state = reconstruct_history(versions, row.tender_version_id)
        version = next(v for v in versions if v.id == row.tender_version_id)
        # Same-version observations can change quality or legacy raw products. Fingerprint
        # those inputs too; historical jobs still use their original observation.
        observation_id = (
            tender.latest_observation_id
            if tender.current_version_id == version.id
            else version.observation_id
        )
        observation = session.get(TenderObservationRow, observation_id)
        assert observation is not None
        pinned = Tender.model_validate(
            {
                **tender.model_dump(),
                **state,
                "current_version_id": version.id,
                "last_seen_at": version.observed_at,
                "latest_observation_id": observation_id,
                "raw_data": observation.raw_data,
                "quality_issues": observation.quality_issues,
            }
        )
        criteria = SqlCampaignRepository(session).criteria(row.criteria_version_id)
        settings = CampaignSettings.model_validate(row.matcher_config)
        if (
            settings.evaluation_context is not None
            and settings.evaluation_context
            != evaluation_context(pinned, criteria.criteria, settings, now)
        ):
            jobs._finish(row, "failed", now, "evaluation_context_changed")
            return None
        return MatchingInput(tender=pinned, criteria=criteria)

    def inputs(self, lease: JobLease, now: datetime) -> MatchingInput | None:
        with self.sessions.begin() as session:
            try:
                return self._eligible(session, lease, now)
            except StaleMatchLease:
                return None

    def complete(self, lease: JobLease, outcome: MatchResult, now: datetime) -> bool:
        with self.sessions.begin() as session:
            jobs = SqlMatchJobRepository(session, _FixedClock(now))
            prior = session.scalar(
                select(MatchResultRow).where(MatchResultRow.job_id == lease.job.id)
            )
            if prior is not None:
                if prior.completion_token != lease.token:
                    return False
                if MatchResult.model_validate(prior.outcome) != outcome:
                    raise IdempotencyConflict("result replay differs")
                return True
            try:
                if not self._eligible(session, lease, now):
                    return False
                job = lease.job
                result = StoredMatchResult(
                    id=uuid5(job.id, "result"),
                    job_id=job.id,
                    tender_id=job.tender_id,
                    tender_version_id=job.tender_version_id,
                    campaign_id=job.campaign_id,
                    criteria_version_id=job.criteria_version_id,
                    matcher_implementation=job.matcher_implementation,
                    matcher_version=job.matcher_version,
                    settings_hash=job.settings_hash,
                    outcome=outcome,
                    created_at=now,
                    completion_token=lease.token,
                )
                jobs.complete(lease, result)
                if self.notify and outcome.relevance == "RELEVANT":
                    from tender_system.operator.telegram import enqueue_notification

                    enqueue_notification(session, result, now)
                return True
            except StaleMatchLease:
                return False

    def fail(
        self, lease: JobLease, error_code: str, now: datetime
    ) -> Literal["retry", "failed", "lost"]:
        with self.sessions.begin() as session:
            jobs = SqlMatchJobRepository(session, _FixedClock(now))
            try:
                row = jobs._owned(lease, now)
            except StaleMatchLease:
                return "lost"
            settings = CampaignSettings.model_validate(row.matcher_config)
            jobs.retry(
                lease,
                now
                + timedelta(
                    seconds=settings.retry.backoff_seconds * 2 ** min(row.attempts - 1, 30)
                ),
                error_code,
            )
            return "failed" if row.status == "failed" else "retry"


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self.instant = now

    def now(self) -> datetime:
        return self.instant
