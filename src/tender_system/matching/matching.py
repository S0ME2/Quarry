"""Campaign workflows, deterministic matcher and batch orchestration; no SQL or scraping."""

import hashlib
import json
from datetime import datetime
from queue import Empty, SimpleQueue
from threading import Thread
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from tender_system.core.configuration import CampaignSettings, Configuration, SourceSettings
from tender_system.core.domain import (
    Campaign,
    CampaignCriteria,
    CriteriaVersion,
    MatchBatchReport,
    MatchJob,
    MatchResult,
    Reason,
    Tender,
)
from tender_system.core.execution import remaining_seconds
from tender_system.core.ports import (
    CampaignRepository,
    Clock,
    Matcher,
    MatcherFactory,
    MatchingWorkStore,
    MatchJobRepository,
    TenderRepository,
    TenderValidityPolicy,
)
from tender_system.core.registry import Registry
from tender_system.core.tenders import (
    ConfigurableTenderValidity,
    IdempotencyConflict,
    normalized_text,
)
from tender_system.matching.matcher_uncertainty import ConservativeRuleEvaluator
from tender_system.matching.semantic_matching import (
    product_text_fields,
    supplementary_text_fields,
    tender_fields,
)


def settings_digest(settings: CampaignSettings) -> str:
    # Bump engine_revision when shared matching behavior changes. Historical results stay immutable.
    behavior: dict[str, object] = dict(
        engine_revision=7 if settings.matcher_adapter == "intelligent" else 4,
        implementation=settings.matcher_adapter,
        version=settings.matcher_version,
        evaluation_context=settings.evaluation_context,
        manual_evaluation_id=str(settings.manual_evaluation_id)
        if settings.manual_evaluation_id
        else None,
    )
    if settings.matcher_adapter == "rules":
        behavior.update(
            match_threshold=settings.match_threshold,
            no_match_threshold=settings.no_match_threshold,
            exclude_expired=settings.routing.exclude_expired,
            excluded_statuses=settings.routing.excluded_statuses,
            uncertainty=settings.uncertainty.model_dump(mode="json"),
        )
    else:
        behavior.update(
            routing=settings.routing.model_dump(mode="json", exclude={"stronger_model"}),
            thresholds=settings.thresholds.model_dump(mode="json"),
            budget_limit=settings.budget_limit,
            match_threshold=settings.match_threshold,
            no_match_threshold=settings.no_match_threshold,
        )
        if settings.matcher_version != "1":
            behavior["uncertainty"] = settings.uncertainty.model_dump(mode="json")
        for key, model in (
            ("model", settings.model),
            ("stronger_model", settings.routing.stronger_model),
        ):
            behavior[key] = (
                None
                if model is None
                else model.model_dump(
                    mode="json",
                    exclude={
                        "api_key_env",
                        "timeout_seconds",
                        "request_interval_seconds",
                        "max_attempts",
                    },
                )
            )
    return hashlib.sha256(json.dumps(behavior, sort_keys=True).encode()).hexdigest()


def evaluation_context(
    tender: Tender, criteria: CampaignCriteria, settings: CampaignSettings, now: datetime
) -> str:
    """Only deadline boundaries change eligibility with time; avoid daily cache churn."""
    deadline = tender.deadline
    days = None if deadline is None else (deadline - now).total_seconds() / 86400
    rules = criteria.deadline_rules
    state = dict(
        fields=tender_fields(tender),
        required=tender.model_dump(mode="json", include=set(settings.uncertainty.required_fields)),
        quality=[issue.model_dump(mode="json") for issue in tender.quality_issues],
        expired=bool(settings.routing.exclude_expired and deadline is not None and deadline <= now),
        below_minimum=bool(
            days is not None
            and rules.min_days_remaining is not None
            and days < rules.min_days_remaining
        ),
        above_maximum=bool(
            days is not None
            and rules.max_days_remaining is not None
            and days > rules.max_days_remaining
        ),
    )
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def manual_matching_settings(
    settings: CampaignSettings, semantic: bool, evaluation_id: UUID | None = None
) -> CampaignSettings:
    """Shared manual request protections for CLI and web; runtime permissions stay at callers."""
    return settings.model_copy(
        update={
            "matcher_adapter": "intelligent" if semantic else "rules",
            "matcher_version": "3" if semantic else "1",
            "manual_evaluation_id": evaluation_id,
            "evaluation_context": None,
            "model": settings.model.model_copy(
                update={
                    "max_attempts": 1,
                    "max_output_tokens": min(settings.model.max_output_tokens or 600, 600),
                }
            ),
            "routing": settings.routing.model_copy(
                update={
                    "semantic_enabled": semantic,
                    "escalation_enabled": False,
                    "max_model_calls": 1,
                }
            ),
            "retry": settings.retry.model_copy(update={"max_attempts": 1}),
        }
    )


class MatchingPlanner:
    def __init__(
        self,
        campaigns: CampaignRepository,
        jobs: MatchJobRepository,
        tenders: TenderRepository,
        config: Configuration,
        validity: TenderValidityPolicy | None = None,
        *,
        automatic: bool = True,
    ) -> None:
        self.campaigns, self.jobs, self.tenders, self.config = campaigns, jobs, tenders, config
        self.validity = validity or ConfigurableTenderValidity()
        self.automatic = automatic
        self._campaign_inputs: dict[
            tuple[UUID, UUID], tuple[CampaignSettings, CampaignCriteria]
        ] = {}

    def _job(self, tender: Tender, campaign: Campaign, now: datetime) -> MatchJob:
        key = (campaign.id, campaign.current_criteria_version_id)
        if key not in self._campaign_inputs:
            self._campaign_inputs[key] = (
                self.config.for_campaign(str(campaign.id)),
                self.campaigns.criteria(campaign.current_criteria_version_id).criteria,
            )
        settings, criteria = self._campaign_inputs[key]
        settings = settings.model_copy(
            update={"evaluation_context": evaluation_context(tender, criteria, settings, now)}
        )
        digest = settings_digest(settings)
        identity = "/".join(
            map(
                str,
                (
                    tender.internal_id,
                    tender.current_version_id,
                    campaign.id,
                    campaign.current_criteria_version_id,
                    settings.matcher_adapter,
                    settings.matcher_version,
                    digest,
                ),
            )
        )
        return MatchJob(
            id=uuid5(NAMESPACE_URL, identity),
            tender_id=tender.internal_id,
            tender_version_id=tender.current_version_id,
            campaign_id=campaign.id,
            criteria_version_id=campaign.current_criteria_version_id,
            matcher_implementation=settings.matcher_adapter,
            matcher_version=settings.matcher_version,
            matcher_config=cast(dict[str, JsonValue], settings.model_dump(mode="json")),
            settings_hash=digest,
            created_at=now,
            available_at=now,
            max_attempts=settings.retry.max_attempts,
        )

    def for_pairs(
        self, pairs: tuple[tuple[UUID, UUID], ...], now: datetime
    ) -> tuple[MatchJob, ...]:
        """Plan explicit pairs with the same validity and pinned settings as normal work."""
        jobs = []
        for tender_id, campaign_id in pairs:
            tender = self.tenders.get(tender_id)
            campaign = self.campaigns.get(campaign_id)
            if tender is None or campaign is None or not campaign.active:
                raise ValueError("active campaign and stored tender required")
            if not self.validity.evaluate(
                tender, self.config.for_matching_source(tender.source_id, str(campaign_id)), now
            ).matchable:
                raise ValueError("selected tender is invalid for matching")
            jobs.append(self._job(tender, campaign, now))
        return self.jobs.enqueue(tuple(jobs))

    def on_tenders(
        self, tenders: tuple[Tender, ...], settings: SourceSettings, now: datetime
    ) -> None:
        campaigns = self.campaigns.active()
        self.jobs.enqueue(
            tuple(
                self._job(tender, campaign, now)
                for tender in tenders
                for campaign in campaigns
                if self.validity.evaluate(
                    tender,
                    self.config.for_matching_source(
                        tender.source_id, str(campaign.id), source_settings=settings
                    ),
                    now,
                ).matchable
            )
        )

    def for_campaign(self, campaign: Campaign, now: datetime) -> None:
        if not self.automatic or not campaign.active:
            return
        after: UUID | None = None
        size = self.config.for_campaign(str(campaign.id)).batch_size
        while batch := self.tenders.scan(after, size):
            self.jobs.enqueue(
                tuple(
                    self._job(tender, campaign, now)
                    for tender in batch
                    if self.validity.evaluate(
                        tender,
                        self.config.for_matching_source(tender.source_id, str(campaign.id)),
                        now,
                    ).matchable
                )
            )
            after = batch[-1].internal_id


class CampaignService:
    """Caller owns one transaction containing campaign, criteria and all backfill jobs."""

    def __init__(
        self, campaigns: CampaignRepository, planner: MatchingPlanner, clock: Clock
    ) -> None:
        self.campaigns, self.planner, self.clock = campaigns, planner, clock

    def _criteria(self, campaign_id: UUID, criteria: CampaignCriteria) -> CampaignCriteria:
        if "matching_mode" not in criteria.model_fields_set:
            return criteria.model_copy(
                update={
                    "matching_mode": self.planner.config.for_campaign(
                        str(campaign_id)
                    ).matching_mode
                }
            )
        return criteria

    def create(
        self,
        campaign_id: UUID,
        version_id: UUID,
        criteria: CampaignCriteria,
        *,
        active: bool = True,
    ) -> Campaign:
        with self.campaigns.atomic():
            criteria = self._criteria(campaign_id, criteria)
            prior = self.campaigns.get(campaign_id)
            if prior is not None:
                version = self.campaigns.criteria(version_id)
                if (
                    version.campaign_id != campaign_id
                    or version.criteria != criteria
                    or version.previous_version_id is not None
                ):
                    raise IdempotencyConflict("campaign ID/version reused with different criteria")
                return prior
            version = CriteriaVersion(
                id=version_id,
                campaign_id=campaign_id,
                criteria=criteria,
                created_at=self.clock.now(),
            )
            campaign = Campaign(
                id=campaign_id, active=active, current_criteria_version_id=version_id
            )
            self.campaigns.store(campaign, version)
            stored = self.campaigns.get(campaign_id)
            assert stored is not None
            campaign = stored
            return campaign

    def update(
        self,
        campaign_id: UUID,
        version_id: UUID,
        criteria: CampaignCriteria,
        *,
        expected_version_id: UUID | None = None,
    ) -> Campaign:
        with self.campaigns.atomic():
            campaign = self.campaigns.get(campaign_id)
            if campaign is None:
                raise KeyError("campaign not found")
            criteria = self._criteria(campaign_id, criteria)
            try:
                existing = self.campaigns.criteria(version_id)
            except KeyError:
                existing = None
            if existing is not None:
                if (
                    existing.campaign_id != campaign_id
                    or existing.criteria != criteria
                    or (
                        expected_version_id is not None
                        and existing.previous_version_id != expected_version_id
                    )
                ):
                    raise IdempotencyConflict("criteria version ID reused with different input")
                return campaign
            previous = self.campaigns.criteria(campaign.current_criteria_version_id)
            if expected_version_id is not None and previous.id != expected_version_id:
                raise ValueError("criteria baseline changed")
            version = CriteriaVersion(
                id=version_id,
                campaign_id=campaign_id,
                criteria=criteria,
                previous_version_id=previous.id,
                number=previous.number + 1,
                created_at=self.clock.now(),
            )
            campaign = campaign.model_copy(update={"current_criteria_version_id": version_id})
            self.campaigns.store(campaign, version)
            stored = self.campaigns.get(campaign_id)
            assert stored is not None
            campaign = stored
            return campaign

    def activate(self, campaign_id: UUID, active: bool) -> Campaign:
        with self.campaigns.atomic():
            return self.campaigns.set_active(campaign_id, active)

    def rematch(self, campaign_id: UUID) -> None:
        with self.campaigns.atomic():
            campaign = self.campaigns.get(campaign_id)
            if campaign is None:
                raise KeyError("campaign not found")
            self.planner.for_campaign(campaign, self.clock.now())


class RulesMatcher:
    """Reference hard filters and keyword coverage; does not interpret natural-language intent."""

    implementation = "rules"
    version = "1"

    def __init__(self, settings: CampaignSettings, clock: Clock) -> None:
        if settings.matcher_version != self.version:
            raise ValueError("reference rules implementation supports version 1")
        self.settings, self.clock = settings, clock

    def match(self, tender: Tender, criteria: CampaignCriteria) -> MatchResult:
        rules = ConservativeRuleEvaluator(self.settings.uncertainty).evaluate(
            tender, criteria, self.clock.now(), self.settings.routing
        )
        failures = list(rules.failures)
        unknown = list(rules.unknowns)
        if tender.documents_incomplete:
            unknown.append(
                Reason(
                    code="document_processing_incomplete",
                    detail="One or more tender documents could not be processed",
                )
            )
        text = normalized_text(
            " ".join(
                x
                for x in (
                    tender.title,
                    tender.description,
                    tender.document_text,
                    *(value for _, value in supplementary_text_fields(tender)),
                    *(value for _, value in product_text_fields(tender)),
                )
                if x
            )
        )
        keywords = tuple(normalized_text(x) for x in criteria.include_keywords if x.strip())
        score = sum(word in text for word in keywords) / len(keywords) if keywords else 1.0
        if criteria.description or criteria.custom_notes:
            unknown.append(
                Reason(
                    code="natural_language_unassessed",
                    detail="Reference matcher does not interpret prose criteria",
                )
            )
        if (
            criteria.matching_mode == "strict"
            and score < 1
            and not rules.unknowns
            and not tender.documents_incomplete
        ):
            failures.append(
                Reason(
                    code="required_keywords_missing",
                    detail="Strict mode requires all included keywords",
                )
            )
        if failures:
            result, score = "no_match", 0.0
        elif unknown:
            result = "unsure"
        elif criteria.matching_mode == "broad":
            result = "match" if score > 0 else "no_match"
        else:
            result = (
                "match"
                if score >= self.settings.match_threshold
                else "no_match"
                if score <= self.settings.no_match_threshold
                else "unsure"
            )
        if result == "unsure" and not unknown:
            unknown.append(
                Reason(
                    code="keyword_coverage_ambiguous",
                    detail="Coverage lies between configured thresholds",
                )
            )
        return MatchResult.model_validate(
            dict(
                result=result,
                score=score,
                confidence=0.0 if unknown and not failures else 1.0,
                reasons=tuple(failures)
                or (
                    Reason(code="keyword_coverage", detail="Reference keyword coverage evaluated"),
                ),
                uncertainties=tuple(unknown),
                evidence=rules.evidence,
                decision_stage="rules",
                matcher_implementation=self.implementation,
                matcher_version=self.version,
                tender_version_id=tender.current_version_id,
            )
        )


def bounded_match(
    matcher: Matcher, tender: Tender, criteria: CampaignCriteria, timeout: float
) -> MatchResult:
    """A late adapter result cannot reach storage; adapters should also enforce I/O timeouts."""
    replies: SimpleQueue[MatchResult | Exception] = SimpleQueue()

    def invoke() -> None:
        try:
            replies.put(matcher.match(tender.model_copy(deep=True), criteria))
        except Exception as error:
            replies.put(error)

    Thread(target=invoke, daemon=True).start()
    try:
        result = replies.get(timeout=timeout)
    except Empty:
        raise TimeoutError("matcher deadline exceeded") from None
    if isinstance(result, Exception):
        raise result
    return MatchResult.model_validate(result.model_dump())


class BatchMatchingWorker:
    def __init__(
        self, store: MatchingWorkStore, matchers: Registry[MatcherFactory], clock: Clock
    ) -> None:
        self.store, self.matchers, self.clock = store, matchers, clock

    def run_selection(self, limit: int) -> MatchBatchReport:
        """Drain a bounded, explicitly scoped store, claiming one job at a time.

        Manual settings disable automatic retries. Unstarted jobs remain pending on
        interruption; completed results remain reusable on the next explicit run.
        """
        if limit < 1:
            raise ValueError("selection limit must be positive")
        totals = MatchBatchReport().model_dump()
        for _ in range(limit):
            remaining_seconds("matching")
            report = self.run_batch(1)
            for key, value in report.model_dump().items():
                totals[key] += value
            if not report.claimed or report.retried or report.lost_leases:
                break
        return MatchBatchReport(**totals)

    def run_batch(self, limit: int = 100) -> MatchBatchReport:
        if limit < 1:
            raise ValueError("batch limit must be positive")
        leases = self.store.claim(limit, self.clock.now())
        counters = dict(
            claimed=len(leases), completed=0, retried=0, failed=0, skipped=0, lost_leases=0
        )
        for lease in leases:
            inputs = self.store.inputs(lease, self.clock.now())
            if inputs is None:
                counters["skipped"] += 1
                continue
            job = lease.job
            try:
                settings = CampaignSettings.model_validate(job.matcher_config)
                matcher = self.matchers.get(job.matcher_implementation)(settings)
                if (
                    matcher.implementation != job.matcher_implementation
                    or matcher.version != job.matcher_version
                ):
                    raise ValueError("matcher implementation/version differs from pinned job")
                outcome = bounded_match(
                    matcher, inputs.tender, inputs.criteria.criteria, settings.timeout_seconds
                )
                bindings = dict(
                    matcher_implementation=job.matcher_implementation,
                    matcher_version=job.matcher_version,
                    tender_version_id=job.tender_version_id,
                    criteria_version_id=job.criteria_version_id,
                )
                if any(
                    getattr(outcome, key) is not None and getattr(outcome, key) != value
                    for key, value in bindings.items()
                ):
                    raise ValueError("matcher output changed pinned versions")
                if (
                    inputs.tender.documents_incomplete
                    and outcome.result in {"match", "no_match"}
                    # Missing documents can change either semantic outcome; preserve
                    # independently evidenced hard-constraint rejections only.
                    and not ConservativeRuleEvaluator(settings.uncertainty)
                    .evaluate(
                        inputs.tender, inputs.criteria.criteria, self.clock.now(), settings.routing
                    )
                    .failures
                ):
                    outcome = outcome.model_copy(
                        update={
                            "result": "unsure",
                            "uncertainties": outcome.uncertainties
                            + (
                                Reason(
                                    code="document_processing_incomplete",
                                    detail="One or more tender documents could not be processed",
                                ),
                            ),
                        }
                    )
                outcome = MatchResult.model_validate({**outcome.model_dump(), **bindings})
            except Exception as error:
                disposition = self.store.fail(lease, type(error).__name__, self.clock.now())
                counters[
                    {"failed": "failed", "retry": "retried", "lost": "lost_leases"}[disposition]
                ] += 1
                continue
            if self.store.complete(lease, outcome, self.clock.now()):
                counters["completed"] += 1
            else:
                counters["lost_leases"] += 1
        return MatchBatchReport(**counters)
