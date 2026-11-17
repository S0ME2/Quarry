import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import JsonValue, SecretStr
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError

from tender_system.collection.adapters import MemoryEvidenceStore
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.collection.recipes import RecipeExecutor, transforms
from tender_system.collection.source_runs import SourceRunService
from tender_system.core.configuration import CampaignSettings, Configuration
from tender_system.core.domain import (
    Campaign,
    CampaignCriteria,
    MatchJob,
    MatchResult,
    SourceRecipe,
    SourceRecord,
    StoredMatchResult,
)
from tender_system.core.ports import MatcherFactory
from tender_system.core.registry import Registry
from tender_system.matching.intelligent_matching import IntelligentMatcher
from tender_system.matching.matching import BatchMatchingWorker, CampaignService, RulesMatcher
from tender_system.matching.model_providers import GroqModelClient
from tender_system.persistence.matching_storage import (
    SqlCampaignRepository,
    SqlMatchJobRepository,
    TransactionalMatchingStore,
    matching_planner,
)
from tender_system.persistence.storage import (
    CampaignRow,
    CriteriaRow,
    MatchJobRow,
    OperationRow,
    SourceRunRow,
    SqlSourceRepository,
    SqlTenderRepository,
    TransactionalRunStore,
    session_factory,
)
from tender_system.testing.calibration import fixture_source_settings
from tender_system.testing.fakes import (
    FakeClock,
    FunctionMatcher,
    ScriptedHttpAdapter,
    ScriptedMatcher,
)

pytestmark = pytest.mark.database
NOW = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = SourceRecord(id="s", name="Matching fixture source")


def row(id: str, **changes: JsonValue) -> dict[str, JsonValue]:
    return {"external_tender_id": id, "title": "Network routers", **changes}


class Pipeline:
    def __init__(self, engine: Engine, config: Configuration | None = None) -> None:
        self.sessions = session_factory(engine)
        self.clock = FakeClock(NOW)
        self.config = config or Configuration.model_validate(
            {
                "global": {
                    "source": fixture_source_settings().model_dump(),
                    "campaign": {
                        "batch_size": 10,
                        "timeout_seconds": 0.1,
                        "lease_seconds": 20,
                        "retry": {"backoff_seconds": 0.01},
                    },
                }
            }
        )
        self.store = TransactionalMatchingStore(self.sessions, self.config)
        self.source_store = TransactionalRunStore(
            self.sessions, clock=self.clock, configuration=self.config
        )
        with self.sessions.begin() as session:
            SqlSourceRepository(session).add(SOURCE)

    def publish(self, rows: list[dict[str, JsonValue]], run_id: UUID | None = None) -> None:
        self.clock.instant += timedelta(seconds=1)
        recipe = SourceRecipe.model_validate(
            {
                "source_id": "s",
                "version": "1",
                "coverage": "incremental",
                "expected_count": len(rows),
                "completion_signal": {"kind": "total_reconciled", "value": len(rows)},
                "steps": [
                    {
                        "type": "transform",
                        "parameters": {"name": "identity", "input": rows, "emit": True},
                    }
                ],
            }
        )
        settings = self.config.for_source("s")
        service = SourceRunService(
            RecipeExecutor(
                ScriptedHttpAdapter(()), MemoryEvidenceStore(), settings, clock=self.clock
            ),
            ConfigurableTenderNormalizer(),
            self.source_store,
            clock=self.clock,
        )
        result = service.run(run_id or uuid4(), recipe, SOURCE, settings)
        assert result.status == "accepted", result
        # Engine regression harness: consume publication outbox by explicitly planning
        # jobs; production automatic execution is covered in test_automatic_matching.
        with self.sessions.begin() as session:
            for operation in session.scalars(
                select(OperationRow).where(
                    OperationRow.kind == "automatic_matching", OperationRow.status == "queued"
                )
            ):
                repo = SqlTenderRepository(session)
                tenders = tuple(
                    t
                    for key in cast(list[str], operation.payload["tender_ids"])
                    if (t := repo.get(UUID(str(key)))) is not None
                )
                matching_planner(session, self.config).on_tenders(
                    tenders, settings, self.clock.now()
                )
                operation.status = "completed"

    def history_size(self) -> int:
        with self.sessions() as session:
            repository = SqlTenderRepository(session)
            return sum(len(repository.history(t.internal_id)) for t in repository.for_source("s"))

    def campaign(
        self,
        criteria: CampaignCriteria | None = None,
        *,
        active: bool = True,
        id: UUID | None = None,
        version_id: UUID | None = None,
    ) -> Campaign:
        with self.sessions.begin() as session:
            repository = SqlCampaignRepository(session)
            campaign = CampaignService(
                repository, matching_planner(session, self.config), self.clock
            ).create(
                id or uuid4(),
                version_id or uuid4(),
                (criteria or CampaignCriteria(name="IT")).model_copy(
                    update={"automatic_matching": True}
                ),
                active=active,
            )
            matching_planner(session, self.config).for_campaign(campaign, self.clock.now())
            return campaign

    def update(
        self, campaign: Campaign, criteria: CampaignCriteria, version_id: UUID | None = None
    ) -> Campaign:
        with self.sessions.begin() as session:
            updated = CampaignService(
                SqlCampaignRepository(session), matching_planner(session, self.config), self.clock
            ).update(
                campaign.id,
                version_id or uuid4(),
                criteria.model_copy(update={"automatic_matching": True}),
            )
            matching_planner(session, self.config).for_campaign(updated, self.clock.now())
            return updated

    def activate(self, campaign: Campaign, active: bool) -> Campaign:
        with self.sessions.begin() as session:
            updated = CampaignService(
                SqlCampaignRepository(session), matching_planner(session, self.config), self.clock
            ).activate(campaign.id, active)
            if active:
                matching_planner(session, self.config).for_campaign(updated, self.clock.now())
            return updated

    def worker(
        self, factory: MatcherFactory | None = None, name: str = "rules"
    ) -> BatchMatchingWorker:
        registry: Registry[MatcherFactory] = Registry()
        registry.register(name, factory or (lambda settings: RulesMatcher(settings, self.clock)))
        return BatchMatchingWorker(self.store, registry, self.clock)

    def jobs(self) -> tuple[MatchJob, ...]:
        with self.sessions() as session:
            ids = session.scalars(
                select(MatchJobRow.id).order_by(MatchJobRow.created_at, MatchJobRow.id)
            )
            repository = SqlMatchJobRepository(session)
            return tuple(job for key in ids if (job := repository.get(key)) is not None)

    def results(self) -> tuple[StoredMatchResult, ...]:
        with self.sessions() as session:
            return SqlMatchJobRepository(session).results()


@pytest.fixture
def pipeline(migrated_engine: Engine) -> Pipeline:
    return Pipeline(migrated_engine)


def fake_config(**overrides: object) -> Configuration:
    return Configuration.model_validate(
        {
            "global": {
                "source": fixture_source_settings().model_dump(),
                "campaign": {
                    "matcher_adapter": "fake",
                    "batch_size": 10,
                    "timeout_seconds": 0.1,
                    "lease_seconds": 20,
                    "retry": {"backoff_seconds": 0.01},
                    **overrides,
                },
            }
        }
    )


def test_cached_deadline_window_reopens_without_daily_reevaluations(pipeline: Pipeline) -> None:
    from tender_system.core.domain import DeadlineRules

    pipeline.publish([row("time", deadline=(NOW + timedelta(days=5)).isoformat())])
    campaign = pipeline.campaign(
        CampaignCriteria(
            name="Window", deadline_rules=DeadlineRules(min_days_remaining=1, max_days_remaining=3)
        )
    )
    assert pipeline.worker().run_batch().completed == 1
    assert pipeline.results()[0].outcome.result == "no_match"

    def rematch() -> None:
        with pipeline.sessions.begin() as session:
            matching_planner(session, pipeline.config).for_campaign(campaign, pipeline.clock.now())

    pipeline.clock.instant = NOW + timedelta(days=1)
    rematch()
    assert pipeline.worker().run_batch().claimed == 0
    assert len(pipeline.jobs()) == 1
    pipeline.clock.instant = NOW + timedelta(days=2)
    rematch()
    assert pipeline.worker().run_batch().completed == 1
    assert pipeline.results()[-1].outcome.result == "match"
    assert len({r.tender_version_id for r in pipeline.results()}) == 1
    pipeline.clock.instant = NOW + timedelta(days=4, seconds=1)
    rematch()
    assert pipeline.worker().run_batch().completed == 1
    assert pipeline.results()[-1].outcome.result == "no_match"
    assert len(pipeline.results()) == 3


def test_queued_deadline_context_cannot_cache_a_later_decision(pipeline: Pipeline) -> None:
    from tender_system.core.domain import DeadlineRules

    pipeline.publish([row("time", deadline=(NOW + timedelta(days=5)).isoformat())])
    campaign = pipeline.campaign(
        CampaignCriteria(name="Window", deadline_rules=DeadlineRules(max_days_remaining=3))
    )
    pipeline.clock.instant = NOW + timedelta(days=2)
    report = pipeline.worker().run_batch()
    assert report.skipped == 1 and report.completed == 0
    assert pipeline.jobs()[0].last_error_code == "evaluation_context_changed"
    with pipeline.sessions.begin() as session:
        matching_planner(session, pipeline.config).for_campaign(campaign, pipeline.clock.now())
    assert pipeline.worker().run_batch().completed == 1
    assert pipeline.results()[0].outcome.result == "match"


def test_concurrent_identical_selections_call_fake_model_once(migrated_engine: Engine) -> None:
    from tender_system.core.ports import ModelRequest, ModelResponse
    from tender_system.testing.fakes import FunctionModelClient

    config = fake_config(
        matcher_adapter="intelligent", matcher_version="3", timeout_seconds=5, lease_seconds=100
    )
    pipeline = Pipeline(migrated_engine, config)
    pipeline.publish([row("same")])
    campaign = pipeline.campaign(CampaignCriteria(name="Networking", include_keywords=("router",)))
    tender_id = pipeline.jobs()[0].tender_id

    def plan() -> UUID:
        with pipeline.sessions.begin() as session:
            jobs = matching_planner(session, config).for_pairs(
                ((tender_id, campaign.id),), pipeline.clock.now()
            )
            return jobs[0].id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = tuple(pool.map(lambda _: plan(), range(2)))
    assert ids[0] == ids[1] and len(pipeline.jobs()) == 1
    entered, release = threading.Event(), threading.Event()
    calls: list[ModelRequest] = []

    def answer(request: ModelRequest) -> ModelResponse:
        calls.append(request)
        entered.set()
        assert release.wait(timeout=4)
        return ModelResponse(
            {
                "decision": "match",
                "confidence": 0.99,
                "explanation": "Router",
                "evidence": [{"field": "title", "quote": "Network routers"}],
            },
            "fixture",
        )

    worker = pipeline.worker(
        lambda settings: IntelligentMatcher(
            settings, pipeline.clock, lambda _: FunctionModelClient(answer)
        ),
        "intelligent",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker.run_batch, 1)
        try:
            assert entered.wait(timeout=4)
            assert worker.run_batch(1).claimed == 0
        finally:
            release.set()
        assert first.result().completed == 1
    assert len(calls) == 1 and len(pipeline.results()) == 1
    plan()
    assert worker.run_batch(1).claimed == 0 and len(calls) == 1


def matching_reuse_workload(engine: Engine) -> dict[str, object]:
    """Fixed PostgreSQL + fake v3 workload, also runnable against the baseline checkout."""
    from time import perf_counter

    from tender_system.core.ports import ModelRequest, ModelResponse
    from tender_system.testing.fakes import FunctionModelClient

    config = fake_config(matcher_adapter="intelligent", matcher_version="3")
    source = config.global_settings.source
    # This fixture deliberately changes every URL; collection's mass-change guard
    # is independently tested and must not obscure matching reuse measurements.
    config = config.model_copy(
        update={
            "global_settings": config.global_settings.model_copy(
                update={
                    "source": source.model_copy(
                        update={
                            "health": source.health.model_copy(
                                update={"maximum_changed_ratio": None}
                            )
                        }
                    )
                }
            )
        }
    )
    pipeline = Pipeline(engine, config)
    rows = [
        row(
            str(index),
            buyer="Blocked" if index % 4 == 1 else "Allowed",
            currency="UZS" if index % 4 == 2 else "USD",
            value=None if index % 4 == 3 else "500",
            source_url=f"https://example.test/old/{index}",
        )
        for index in range(40)
    ]
    pipeline.publish(rows)
    campaign = pipeline.campaign(
        CampaignCriteria(
            name="Network",
            include_keywords=("router",),
            exclude_buyers=("Blocked",),
            currencies=("USD",),
            min_value=Decimal(100),
        )
    )
    calls: list[ModelRequest] = []

    def answer(request: ModelRequest) -> ModelResponse:
        calls.append(request)
        return ModelResponse(
            {
                "decision": "match",
                "confidence": 0.99,
                "explanation": "Router",
                "evidence": [{"field": "title", "quote": "Network routers"}],
            },
            "fixture",
        )

    worker = pipeline.worker(
        lambda settings: IntelligentMatcher(
            settings, pipeline.clock, lambda _: FunctionModelClient(answer)
        ),
        "intelligent",
    )

    def drain() -> int:
        completed = 0
        while True:
            report = worker.run_batch(10)
            completed += report.completed
            if not report.claimed:
                break
        return completed

    started = perf_counter()
    first = drain()
    pairs = tuple((job.tender_id, campaign.id) for job in pipeline.jobs())

    def plan() -> tuple[MatchJob, ...]:
        with pipeline.sessions.begin() as session:
            return matching_planner(session, pipeline.config).for_pairs(pairs, pipeline.clock.now())

    plan()
    repeated = drain()
    # URLs are tracked history, excluded from all matcher inputs.
    pipeline.publish(
        [
            {**item, "source_url": f"https://example.test/new/{index}"}
            for index, item in enumerate(rows)
        ]
    )
    planned = plan()
    metadata = drain()
    results = pipeline.results()
    return {
        "candidate_pairs": 120,
        "first_evaluations": first,
        "unchanged_evaluations": repeated,
        "metadata_evaluations": metadata,
        "actual_evaluations": first + repeated + metadata,
        "cache_reused": 120 - first - repeated - metadata,
        "model_calls": len(calls),
        "safe_rejections": sum(r.outcome.result == "no_match" for r in results),
        "missing_information": sum(r.outcome.result == "unsure" for r in results),
        "unique_results": len(results),
        "history_versions": pipeline.history_size(),
        "planned_ids": len({job.id for job in planned}),
        "seconds": round(perf_counter() - started, 6),
    }


def test_matching_reuses_irrelevant_versions_workload(migrated_engine: Engine) -> None:
    report = matching_reuse_workload(migrated_engine)
    print(json.dumps(report, sort_keys=True))
    assert report["actual_evaluations"] == 40
    assert report["unchanged_evaluations"] == report["metadata_evaluations"] == 0
    assert report["model_calls"] == 10
    assert report["safe_rejections"] == 20
    assert report["missing_information"] == 10
    assert report["unique_results"] == 40 and report["history_versions"] == 80


def test_manual_interruption_keeps_unstarted_work_pending(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tender_system.core.execution import ExecutionDeadlineExceeded

    pipeline.publish([row(str(index)) for index in range(3)])
    pipeline.campaign()
    checks = 0

    def budget(stage: str) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ExecutionDeadlineExceeded(stage)

    monkeypatch.setattr("tender_system.matching.matching.remaining_seconds", budget)
    with pytest.raises(ExecutionDeadlineExceeded):
        pipeline.worker().run_selection(3)
    assert len(pipeline.results()) == 1
    assert sum(job.status == "pending" and job.attempts == 0 for job in pipeline.jobs()) == 2
    assert pipeline.worker().run_selection(3).completed == 2
    assert len(pipeline.results()) == 3


def test_same_version_quality_change_invalidates_reuse(pipeline: Pipeline) -> None:
    from tender_system.core.domain import DataQualityIssue, TenderCandidate, TenderObservation

    pipeline.publish([row("quality")])
    campaign = pipeline.campaign()
    assert pipeline.worker().run_batch().completed == 1
    original = pipeline.results()[0]
    with pipeline.sessions.begin() as session:
        repository = SqlTenderRepository(session)
        item = repository.get(original.tender_id)
        assert item is not None
        candidate = TenderCandidate.model_validate(
            item.model_dump(include=set(TenderCandidate.model_fields))
        )
        candidate = candidate.model_copy(
            update={
                "quality_issues": (
                    DataQualityIssue(
                        field="title", code="unreliable", detail="Fixture quality error"
                    ),
                )
            }
        )
        changed = repository.observe(
            TenderObservation(
                id=uuid4(),
                candidate=candidate,
                observed_at=pipeline.clock.now() + timedelta(seconds=1),
                evidence_reference="fixture:quality",
            ),
            pipeline.config.for_source("s").effective_rematch_fields,
        )
        assert changed.tender.current_version_id == original.tender_version_id
        matching_planner(session, pipeline.config).for_pairs(
            ((item.internal_id, campaign.id),), pipeline.clock.now()
        )
    assert pipeline.worker().run_batch().completed == 1
    assert (
        next(result for result in pipeline.results() if result.id != original.id).outcome.result
        == "unsure"
    )
    assert pipeline.history_size() == 1 and len(pipeline.results()) == 2


def test_changed_model_configuration_creates_new_paid_work(migrated_engine: Engine) -> None:
    from tender_system.core.ports import ModelRequest, ModelResponse
    from tender_system.testing.fakes import FunctionModelClient

    pipeline = Pipeline(
        migrated_engine, fake_config(matcher_adapter="intelligent", matcher_version="3")
    )
    pipeline.publish([row("a")])
    campaign = pipeline.campaign(CampaignCriteria(name="Network", include_keywords=("router",)))
    calls: list[ModelRequest] = []

    def answer(request: ModelRequest) -> ModelResponse:
        calls.append(request)
        return ModelResponse(
            {
                "decision": "match",
                "confidence": 0.99,
                "explanation": "Router",
                "evidence": [{"field": "title", "quote": "Network routers"}],
            },
            "fixture",
        )

    worker = pipeline.worker(
        lambda settings: IntelligentMatcher(
            settings, pipeline.clock, lambda _: FunctionModelClient(answer)
        ),
        "intelligent",
    )
    assert worker.run_batch().completed == 1
    settings = pipeline.config.global_settings.campaign
    settings = settings.model_copy(
        update={"model": settings.model.model_copy(update={"reasoning_effort": "high"})}
    )
    pipeline.config = pipeline.config.model_copy(
        update={
            "global_settings": pipeline.config.global_settings.model_copy(
                update={"campaign": settings}
            )
        }
    )
    with pipeline.sessions.begin() as session:
        matching_planner(session, pipeline.config).for_campaign(campaign, pipeline.clock.now())
    assert worker.run_batch().completed == 1
    assert len(calls) == len(pipeline.results()) == 2


def test_new_valid_tender_jobs_only_for_active_campaigns_and_replay(pipeline: Pipeline) -> None:
    active = pipeline.campaign()
    pipeline.campaign(active=False)
    run_id = uuid4()
    pipeline.publish([row("a")], run_id)
    assert len(pipeline.jobs()) == 1 and pipeline.jobs()[0].campaign_id == active.id
    pipeline.publish([row("a")], run_id)
    assert len(pipeline.jobs()) == 1
    assert pipeline.worker().run_batch().completed == 1
    assert pipeline.worker().run_batch().claimed == 0
    assert len(pipeline.results()) == 1


def test_product_only_change_rematches_pinned_products(pipeline: Pipeline) -> None:
    pipeline.campaign(CampaignCriteria(name="Routers", include_keywords=("router",)))
    pipeline.publish([row("a", title="Equipment", product_items=[{"name": "Router"}])])
    assert pipeline.worker().run_batch().completed == 1
    first = pipeline.results()[0]
    assert first.outcome.result == "match"
    pipeline.publish([row("a", title="Equipment", product_items=[{"name": "Switch"}])])
    assert len(pipeline.jobs()) == 2
    assert pipeline.worker().run_batch().completed == 1
    second = next(result for result in pipeline.results() if result.id != first.id)
    assert second.outcome.result == "no_match"
    assert second.tender_version_id != first.tender_version_id
    pipeline.publish([row("a", title="Equipment", product_items=[{"name": "Switch"}])])
    assert len(pipeline.jobs()) == 2 and pipeline.history_size() == 2


@pytest.mark.parametrize(
    "fields", [{"deadline": "2025-12-31T00:00:00Z"}, {"status": "closed"}, {"status": "cancelled"}]
)
def test_invalid_tenders_are_stored_without_normal_jobs(
    pipeline: Pipeline, fields: dict[str, JsonValue]
) -> None:
    pipeline.campaign()
    pipeline.publish([row("a", **fields)])
    assert not pipeline.jobs()
    with pipeline.sessions() as session:
        tender = SqlTenderRepository(session).for_source("s")[0]
        assert (
            tender.external_tender_id == "a"
            and len(SqlTenderRepository(session).history(tender.internal_id)) == 1
        )


def test_non_rematch_change_preserves_job_and_rematch_change_adds_versioned_job(
    pipeline: Pipeline,
) -> None:
    pipeline.campaign()
    pipeline.publish([row("a")])
    pipeline.worker().run_batch()
    original = pipeline.results()[0]
    pipeline.publish([row("a", source_url="https://fixture.invalid/new")])
    assert len(pipeline.jobs()) == 1
    pipeline.publish([row("a", source_url="https://fixture.invalid/new", title="Network switches")])
    assert len(pipeline.jobs()) == 2
    pipeline.worker().run_batch()
    assert len(pipeline.results()) == 2 and original in pipeline.results()
    assert pipeline.results()[-1].tender_version_id != original.tender_version_id


def test_volatile_countdown_preserves_raw_evidence_without_versions_or_jobs(
    pipeline: Pipeline,
) -> None:
    pipeline.campaign()
    pipeline.publish(
        [row("a", time_left=100, view_count=1, scraped_at="first", ui={"loading": True})]
    )
    pipeline.publish(
        [row("a", time_left=90, view_count=2, scraped_at="second", ui={"loading": False})]
    )
    assert pipeline.history_size() == len(pipeline.jobs()) == 1
    with pipeline.sessions() as session:
        repository = SqlTenderRepository(session)
        tender = repository.for_source("s")[0]
        observations = repository.observations(tender.internal_id)
        assert [o.candidate.raw_data["time_left"] for o in observations] == [100, 90]
        assert [o.candidate.raw_data["view_count"] for o in observations] == [1, 2]
        assert tender.raw_data["scraped_at"] == "second"


@pytest.mark.parametrize(
    "policy,versions,jobs",
    [
        ("IGNORE", 1, 1),
        ("VOLATILE", 1, 1),
        ("TRACK", 2, 2),
        ("REMATCH", 2, 2),
    ],
)
@pytest.mark.parametrize("field", ["title", "buyer"])
def test_source_field_policy_controls_history_and_jobs(
    migrated_engine: Engine, policy: str, versions: int, jobs: int, field: str
) -> None:
    values = fake_config(matcher_adapter="rules").model_dump(by_alias=True)
    values["sources"] = {"s": {"field_change_policy": {field: policy}}}
    pipe = Pipeline(migrated_engine, Configuration.model_validate(values))
    pipe.campaign()
    pipe.publish([row("a")])
    pipe.publish([row("a", **{field: "Changed"})])
    assert pipe.history_size() == versions and len(pipe.jobs()) == jobs
    with pipe.sessions() as session:
        repository = SqlTenderRepository(session)
        tender = repository.for_source("s")[0]
        assert (
            getattr(repository.observations(tender.internal_id)[-1].candidate, field) == "Changed"
        )
        initial = "Network routers" if field == "title" else None
        assert getattr(tender, field) == ("Changed" if versions == 2 else initial)
        if versions == 2:
            assert repository.history(tender.internal_id)[-1].changed_fields == {field: "Changed"}


def test_countdown_normalized_to_deadline_only_real_change_versions_and_rematches(
    pipeline: Pipeline,
) -> None:
    pipeline.campaign()
    transform = transforms().get("deadline_from_remaining")
    for seconds, anchor in [(100, "2026-01-01T00:00:00Z"), (90, "2026-01-01T00:00:10Z")]:
        deadline = transform(seconds, {"observed_at": anchor})
        pipeline.publish([row("a", time_left=seconds, deadline=deadline)])
    assert pipeline.history_size() == len(pipeline.jobs()) == 1
    deadline = transform(100, {"observed_at": "2026-01-01T00:00:10Z"})
    pipeline.publish([row("a", time_left=100, deadline=deadline)])
    assert pipeline.history_size() == len(pipeline.jobs()) == 2
    with pipeline.sessions() as session:
        repository = SqlTenderRepository(session)
        tender = repository.for_source("s")[0]
        assert repository.history(tender.internal_id)[-1].changed_fields == {
            "deadline": "2026-01-01T00:01:50+00:00"
        }


def test_campaign_validity_override_applies_to_backfill_publication_and_worker(
    migrated_engine: Engine,
) -> None:
    values = fake_config(matcher_adapter="rules").model_dump(by_alias=True)
    campaign_id = uuid4()
    values["campaigns"] = {
        str(campaign_id): {"validity": {"exclude_expired": False, "excluded_statuses": []}}
    }
    pipe = Pipeline(migrated_engine, Configuration.model_validate(values))
    pipe.publish([row("old", deadline="2025-01-01T00:00:00Z", status="closed")])
    pipe.campaign(id=campaign_id)
    pipe.campaign()
    pipe.publish([row("new", deadline="2025-01-01T00:00:00Z", status="closed")])
    assert len(pipe.jobs()) == 2 and all(j.campaign_id == campaign_id for j in pipe.jobs())
    assert pipe.worker().run_batch().completed == 2
    assert pipe.config.for_source("s").validity.exclude_expired


def test_unchanged_reappearance_recovers_invalid_job_without_fake_history(
    pipeline: Pipeline,
) -> None:
    from tender_system.persistence.storage import TenderRow

    pipeline.publish([row("a")])
    pipeline.campaign()
    with pipeline.sessions.begin() as session:
        tender = session.scalar(select(TenderRow))
        assert tender is not None
        tender.missing_successful_runs = 3
    assert pipeline.worker().run_batch().skipped == 1
    assert pipeline.jobs()[0].last_error_code == "tender_invalid"
    pipeline.publish([row("a")])
    assert pipeline.history_size() == len(pipeline.jobs()) == 1
    assert pipeline.jobs()[0].status == "pending"
    assert pipeline.worker().run_batch().completed == 1
    pipeline.publish([row("a")])
    assert pipeline.worker().run_batch().claimed == 0 and len(pipeline.results()) == 1


def test_unchanged_reappearance_backfills_campaign_created_while_missing(
    pipeline: Pipeline,
) -> None:
    from tender_system.persistence.storage import TenderRow

    pipeline.publish([row("a")])
    with pipeline.sessions.begin() as session:
        tender = session.scalar(select(TenderRow))
        assert tender is not None
        tender.missing_successful_runs = 3
    pipeline.campaign()
    assert not pipeline.jobs()
    pipeline.publish([row("a")])
    assert pipeline.history_size() == len(pipeline.jobs()) == 1
    assert pipeline.worker().run_batch().completed == 1


def test_matching_inputs_are_not_suppressed_by_legacy_rematch_fields(
    migrated_engine: Engine,
) -> None:
    values = fake_config().model_dump(by_alias=True)
    values["global"]["source"]["rematch_fields"] = ["buyer"]
    pipe = Pipeline(migrated_engine, Configuration.model_validate(values))
    pipe.campaign()
    pipe.publish([row("a")])
    pipe.publish([row("a", title="Switches")])
    assert len(pipe.jobs()) == 2
    pipe.publish([row("a", title="Switches", buyer="Buyer")])
    assert len(pipe.jobs()) == 3


def test_explicit_backfill_matches_only_valid_stored_tenders_without_collection(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline.publish(
        [
            row("valid"),
            row("closed", status="closed"),
            row("expired", deadline="2025-12-31T00:00:00Z"),
        ]
    )

    def no_collection(*args: object, **kwargs: object) -> None:
        raise AssertionError("campaign must not rescrape")

    monkeypatch.setattr(SourceRunService, "run", no_collection)
    campaign = pipeline.campaign()
    assert len(pipeline.jobs()) == 1
    assert pipeline.worker().run_batch().completed == 1
    assert pipeline.results()[0].campaign_id == campaign.id
    with pipeline.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SourceRunRow)) == 1


def test_criteria_versions_and_old_results_survive_updates_and_exact_replay(
    pipeline: Pipeline,
) -> None:
    pipeline.publish([row("a"), row("b")])
    campaign = pipeline.campaign(CampaignCriteria(name="IT", include_keywords=("routers",)))
    assert pipeline.worker().run_batch().completed == 2
    old = pipeline.results()
    new_id = uuid4()
    criteria = CampaignCriteria(name="IT", include_keywords=("cement",))
    updated = pipeline.update(campaign, criteria, new_id)
    assert pipeline.worker().run_batch().completed == 2
    assert len(pipeline.results()) == 4 and all(result in pipeline.results() for result in old)
    assert all(
        r.outcome.result == "no_match"
        for r in pipeline.results()
        if r.criteria_version_id == new_id
    )
    pipeline.update(updated, criteria, new_id)
    assert len(pipeline.jobs()) == 4 and len(pipeline.results()) == 4
    with pipeline.sessions() as session:
        repo = SqlCampaignRepository(session)
        assert repo.criteria(new_id).previous_version_id == campaign.current_criteria_version_id
        assert repo.criteria(new_id).number == 2
        assert repo.criteria(campaign.current_criteria_version_id).criteria.include_keywords == (
            "routers",
        )


def test_queued_old_criteria_cannot_run_after_update(pipeline: Pipeline) -> None:
    pipeline.publish([row("a")])
    campaign = pipeline.campaign()
    old_lease = pipeline.store.claim(1, pipeline.clock.now())[0]
    old_input = pipeline.store.inputs(old_lease, pipeline.clock.now())
    assert old_input is not None
    outcome = RulesMatcher(CampaignSettings(), pipeline.clock).match(
        old_input.tender, old_input.criteria.criteria
    )
    pipeline.update(campaign, CampaignCriteria(name="Changed"))
    assert not pipeline.store.complete(old_lease, outcome, pipeline.clock.now())
    assert not pipeline.results()
    assert pipeline.worker().run_batch().completed == 1
    assert pipeline.results()[0].criteria_version_id != campaign.current_criteria_version_id
    assert any(j.last_error_code == "criteria_superseded" for j in pipeline.jobs())


def test_worker_uses_pinned_tender_version_after_later_non_rematch_change(
    pipeline: Pipeline,
) -> None:
    pipeline.publish([row("a", buyer="Original")])
    pipeline.campaign()
    old_job = pipeline.jobs()[0]
    pipeline.publish([row("a", buyer="Changed")])
    lease = pipeline.store.claim(1, pipeline.clock.now())[0]
    inputs = pipeline.store.inputs(lease, pipeline.clock.now())
    assert inputs is not None and inputs.tender.buyer == "Original"
    assert inputs.tender.current_version_id == old_job.tender_version_id


def test_matcher_and_input_versions_recorded_with_all_three_outcomes(
    migrated_engine: Engine,
) -> None:
    pipe = Pipeline(migrated_engine, fake_config(matcher_version="fake-v2"))
    pipe.publish([row("a"), row("b"), row("c")])
    pipe.campaign()
    fake = ScriptedMatcher(
        tuple(
            MatchResult.model_validate(dict(result=value, score=0.5, reasons=(), uncertainties=()))
            for value in ("match", "no_match", "unsure")
        ),
        version="fake-v2",
    )
    assert pipe.worker(lambda settings: fake, "fake").run_batch().completed == 3
    assert {r.outcome.result for r in pipe.results()} == {"match", "no_match", "unsure"}
    for result in pipe.results():
        assert result.matcher_implementation == result.outcome.matcher_implementation == "fake"
        assert result.matcher_version == result.outcome.matcher_version == "fake-v2"
        assert result.tender_version_id == result.outcome.tender_version_id
        assert result.criteria_version_id == result.outcome.criteria_version_id


@pytest.mark.parametrize(
    "failure", [TimeoutError("private failure"), RuntimeError("private failure")]
)
def test_matcher_failure_retries_and_recovers_without_duplicate_results(
    migrated_engine: Engine, failure: Exception
) -> None:
    pipe = Pipeline(migrated_engine, fake_config())
    pipe.publish([row("a")])
    pipe.campaign()
    fake = ScriptedMatcher(
        (failure, MatchResult(result="match", score=1, reasons=(), uncertainties=()))
    )
    worker = pipe.worker(lambda settings: fake, "fake")
    assert worker.run_batch().retried == 1
    assert not pipe.results() and pipe.jobs()[0].last_error_code == type(failure).__name__
    assert worker.run_batch().claimed == 0
    pipe.clock.instant += timedelta(seconds=1)
    assert worker.run_batch().completed == 1 and len(pipe.results()) == 1
    assert pipe.jobs()[0].attempts == 2


def test_retry_budget_and_crash_recovery_are_bounded(migrated_engine: Engine) -> None:
    pipe = Pipeline(
        migrated_engine, fake_config(retry={"max_attempts": 2, "backoff_seconds": 0.01})
    )
    pipe.publish([row("a")])
    pipe.campaign()
    crashed = pipe.store.claim(1, pipe.clock.now())[0]
    assert pipe.store.claim(1, pipe.clock.now()) == ()
    pipe.clock.instant = crashed.expires_at + timedelta(seconds=1)
    recovered = pipe.store.claim(1, pipe.clock.now())[0]
    assert recovered.job.id == crashed.job.id and recovered.token != crashed.token
    assert not pipe.store.complete(
        crashed,
        MatchResult(result="match", score=1, reasons=(), uncertainties=()),
        pipe.clock.now(),
    )
    pipe.clock.instant = recovered.expires_at + timedelta(seconds=1)
    assert pipe.store.claim(1, pipe.clock.now()) == ()
    assert pipe.jobs()[0].status == "failed" and pipe.jobs()[0].last_error_code == "retry_exhausted"


def test_concurrent_workers_claim_distinct_jobs(pipeline: Pipeline) -> None:
    pipeline.publish([row(str(i)) for i in range(20)])
    pipeline.campaign()
    barrier = threading.Barrier(2)

    def claim() -> tuple[UUID, ...]:
        barrier.wait(timeout=5)
        return tuple(lease.job.id for lease in pipeline.store.claim(10, pipeline.clock.now()))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        first, second = (f.result(timeout=10) for f in futures)
    assert len(first) == len(second) == 10 and not set(first) & set(second)


def test_activation_pauses_and_backfills_without_duplicating_work(pipeline: Pipeline) -> None:
    pipeline.publish([row("a")])
    inactive = pipeline.campaign(active=False)
    assert not pipeline.jobs()
    active = pipeline.activate(inactive, True)
    assert len(pipeline.jobs()) == 1
    pipeline.activate(active, False)
    assert pipeline.worker().run_batch().claimed == 0
    pipeline.publish([row("b")])
    assert len(pipeline.jobs()) == 1
    pipeline.activate(active, True)
    assert len(pipeline.jobs()) == 2
    assert pipeline.worker().run_batch().completed == 2
    pipeline.activate(active, True)
    assert len(pipeline.jobs()) == 2


def test_deactivation_while_matcher_runs_discards_late_result_and_preserves_job(
    pipeline: Pipeline,
) -> None:
    pipeline.publish([row("a")])
    campaign = pipeline.campaign()
    lease = pipeline.store.claim(1, pipeline.clock.now())[0]
    pipeline.activate(campaign, False)
    assert pipeline.store.inputs(lease, pipeline.clock.now()) is None
    assert pipeline.jobs()[0].status == "pending" and pipeline.jobs()[0].attempts == 0
    pipeline.activate(campaign, True)
    assert pipeline.worker().run_batch().completed == 1


def test_tender_expiring_after_enqueue_cannot_produce_normal_result(pipeline: Pipeline) -> None:
    pipeline.publish([row("a", deadline="2026-01-01T00:00:05Z")])
    pipeline.campaign()
    pipeline.clock.instant += timedelta(seconds=10)
    report = pipeline.worker().run_batch()
    assert report.skipped == 1 and not pipeline.results()
    assert pipeline.jobs()[0].last_error_code == "tender_invalid"


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("campaign_criteria", "number", "number + 1"),
        ("match_results", "outcome", "'{}'::jsonb"),
        ("match_jobs", "matcher_version", "'changed'"),
    ],
)
def test_immutable_history_and_job_inputs_are_database_enforced(
    pipeline: Pipeline, table: str, column: str, value: str
) -> None:
    pipeline.publish([row("a")])
    pipeline.campaign()
    pipeline.worker().run_batch()
    with pytest.raises(DBAPIError), pipeline.sessions.begin() as session:
        session.execute(text(f"UPDATE {table} SET {column} = {value}"))
    if table != "match_jobs":
        with pytest.raises(DBAPIError), pipeline.sessions.begin() as session:
            session.execute(text(f"DELETE FROM {table}"))


def test_campaign_and_job_rollback_leave_no_partial_backfill(pipeline: Pipeline) -> None:
    pipeline.publish([row("a"), row("b")])
    with pytest.raises(RuntimeError), pipeline.sessions.begin() as session:
        CampaignService(
            SqlCampaignRepository(session),
            matching_planner(session, pipeline.config),
            pipeline.clock,
        ).create(uuid4(), uuid4(), CampaignCriteria(name="C"))
        raise RuntimeError("crash before commit")
    assert not pipeline.jobs()
    with pipeline.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CampaignRow)) == 0
        assert session.scalar(select(func.count()).select_from(CriteriaRow)) == 0


def test_large_inventory_processed_in_bounded_batches(pipeline: Pipeline) -> None:
    pipeline.publish([row(str(i)) for i in range(125)])
    pipeline.campaign()
    assert len(pipeline.jobs()) == 125
    worker = pipeline.worker()
    sizes = []
    while report := worker.run_batch(100):
        if not report.claimed:
            break
        sizes.append(report.completed)
    assert sizes == [10] * 12 + [5]
    assert len(pipeline.results()) == 125 and all(j.status == "done" for j in pipeline.jobs())


def test_completion_exact_replay_and_late_old_lease_are_fenced(pipeline: Pipeline) -> None:
    pipeline.publish([row("a")])
    pipeline.campaign()
    first = pipeline.store.claim(1, pipeline.clock.now())[0]
    pipeline.clock.instant = first.expires_at + timedelta(seconds=1)
    lease = pipeline.store.claim(1, pipeline.clock.now())[0]
    inputs = pipeline.store.inputs(lease, pipeline.clock.now())
    assert inputs is not None
    outcome = RulesMatcher(CampaignSettings(), pipeline.clock).match(
        inputs.tender, inputs.criteria.criteria
    )
    outcome = outcome.model_copy(update={"criteria_version_id": lease.job.criteria_version_id})
    assert pipeline.store.complete(lease, outcome, pipeline.clock.now())
    assert pipeline.store.complete(lease, outcome, pipeline.clock.now())
    assert not pipeline.store.complete(first, outcome, pipeline.clock.now())
    assert len(pipeline.results()) == 1
    with pipeline.sessions.begin() as session:
        result = pipeline.results()[0]
        SqlMatchJobRepository(session, pipeline.clock).complete(lease, result)
    assert len(pipeline.results()) == 1


def test_matcher_commit_crash_rolls_back_result_and_recovers(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline.publish([row("a")])
    pipeline.campaign()
    finish = SqlMatchJobRepository._finish

    def crash(
        self: SqlMatchJobRepository,
        row: MatchJobRow,
        status: str,
        now: datetime,
        error: str | None = None,
    ) -> None:
        finish(self, row, status, now, error)
        if status == "done":
            raise RuntimeError("crash after result flush before commit")

    monkeypatch.setattr(SqlMatchJobRepository, "_finish", crash)
    with pytest.raises(RuntimeError):
        pipeline.worker().run_batch()
    assert not pipeline.results() and pipeline.jobs()[0].status == "running"
    monkeypatch.setattr(SqlMatchJobRepository, "_finish", finish)
    pipeline.clock.instant += timedelta(seconds=21)
    assert pipeline.worker().run_batch().completed == 1
    assert len(pipeline.results()) == 1 and pipeline.jobs()[0].attempts == 2


def test_matching_failure_preserves_committed_tender_history(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline.campaign()
    enqueue = SqlMatchJobRepository.enqueue

    def crash(self: SqlMatchJobRepository, jobs: tuple[MatchJob, ...]) -> None:
        enqueue(self, jobs)
        raise RuntimeError("crash after job insertion")

    monkeypatch.setattr(SqlMatchJobRepository, "enqueue", crash)
    with pytest.raises(RuntimeError):
        pipeline.publish([row("a")])
    assert not pipeline.jobs()
    with pipeline.sessions() as session:
        assert len(SqlTenderRepository(session).for_source("s")) == 1
        assert session.scalar(select(func.count()).select_from(SourceRunRow)) == 1
    monkeypatch.setattr(SqlMatchJobRepository, "enqueue", enqueue)
    pipeline.publish([row("a")])
    assert len(pipeline.jobs()) == 1


def test_claim_skips_rows_locked_by_an_uncommitted_worker(pipeline: Pipeline) -> None:
    pipeline.publish([row("a"), row("b")])
    pipeline.campaign()
    with pipeline.sessions.begin() as session:
        first = SqlMatchJobRepository(session, pipeline.clock).claim(pipeline.clock.now(), 20)
        assert first is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            second = executor.submit(pipeline.store.claim, 1, pipeline.clock.now()).result(
                timeout=5
            )
        assert len(second) == 1 and second[0].job.id != first.job.id


def test_campaign_creation_does_not_implicitly_backfill(pipeline: Pipeline) -> None:
    pipeline.publish([row("existing")])
    with pipeline.sessions.begin() as session:
        CampaignService(
            SqlCampaignRepository(session),
            matching_planner(session, pipeline.config),
            pipeline.clock,
        ).create(uuid4(), uuid4(), CampaignCriteria(name="New"))
    assert not pipeline.jobs()


@pytest.mark.parametrize("bad", ["malformed", "wrong_version", "wrong_implementation"])
def test_invalid_matcher_output_never_becomes_a_result(migrated_engine: Engine, bad: str) -> None:
    pipe = Pipeline(migrated_engine, fake_config())
    pipe.publish([row("a")])
    pipe.campaign()
    if bad == "malformed":
        outcome = MatchResult.model_construct(
            result="match",
            score=10,
            reasons=(),
            uncertainties=(),
        )
    elif bad == "wrong_version":
        outcome = MatchResult(
            result="match", score=1, reasons=(), uncertainties=(), criteria_version_id=uuid4()
        )
    else:
        outcome = MatchResult(
            result="match",
            score=1,
            reasons=(),
            uncertainties=(),
            matcher_implementation="unregistered",
        )
    fake = ScriptedMatcher((outcome,))
    assert pipe.worker(lambda settings: fake, "fake").run_batch().retried == 1
    assert not pipe.results()


def test_actual_timeout_discards_late_output_and_next_attempt_can_succeed(
    migrated_engine: Engine,
) -> None:
    from threading import Event

    release = Event()
    pipe = Pipeline(migrated_engine, fake_config(timeout_seconds=0.01))
    pipe.publish([row("a")])
    pipe.campaign()

    def slow(tender: object, criteria: object) -> MatchResult:
        release.wait(timeout=5)
        return MatchResult(result="match", score=1, reasons=(), uncertainties=())

    try:
        assert pipe.worker(lambda settings: FunctionMatcher(slow), "fake").run_batch().retried == 1
        assert pipe.jobs()[0].last_error_code == "TimeoutError"
        assert not pipe.results()
    finally:
        release.set()
    pipe.clock.instant += timedelta(seconds=1)
    assert (
        pipe.worker(
            lambda settings: FunctionMatcher(
                lambda tender, criteria: MatchResult(
                    result="match", score=1, reasons=(), uncertainties=()
                )
            ),
            "fake",
        )
        .run_batch()
        .completed
        == 1
    )
    assert len(pipe.results()) == 1


def test_matcher_configuration_upgrade_creates_new_results_without_overwrite(
    pipeline: Pipeline,
) -> None:
    pipeline.publish([row("a")])
    campaign = pipeline.campaign()
    pipeline.worker().run_batch()
    old = pipeline.results()[0]
    values = pipeline.config.model_dump(by_alias=True)
    values["global"]["campaign"]["matcher_version"] = "2"
    pipeline.config = Configuration.model_validate(values)
    with pipeline.sessions.begin() as session:
        CampaignService(
            SqlCampaignRepository(session),
            matching_planner(session, pipeline.config),
            pipeline.clock,
        ).rematch(campaign.id)
    replacement = FunctionMatcher(
        lambda tender, criteria: MatchResult(
            result="no_match", score=0, reasons=(), uncertainties=()
        ),
        implementation="rules",
        version="2",
    )
    assert pipeline.worker(lambda settings: replacement).run_batch().completed == 1
    assert len(pipeline.results()) == 2 and old in pipeline.results()
    assert {r.matcher_version for r in pipeline.results()} == {"1", "2"}


def test_bulk_publication_chunks_jobs_and_workers_process_all_batches(
    migrated_engine: Engine,
) -> None:
    config = fake_config(matcher_adapter="rules", batch_size=100)
    pipe = Pipeline(migrated_engine, config)
    pipe.campaign()
    pipe.publish([row(str(i)) for i in range(501)])
    assert len(pipe.jobs()) == 501
    worker = pipe.worker()
    batches = [worker.run_batch().completed for _ in range(6)]
    assert batches == [100, 100, 100, 100, 100, 1]
    assert worker.run_batch().claimed == 0 and len(pipe.results()) == 501


def test_campaign_edit_does_not_invoke_backfill(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline.publish([row("a")])
    campaign = pipeline.campaign()
    enqueue = SqlMatchJobRepository.enqueue

    def broken(self: SqlMatchJobRepository, jobs: tuple[MatchJob, ...]) -> None:
        enqueue(self, jobs)
        raise RuntimeError("backfill failed after criteria insertion")

    monkeypatch.setattr(SqlMatchJobRepository, "enqueue", broken)
    with pipeline.sessions.begin() as session:
        service = CampaignService(
            SqlCampaignRepository(session),
            matching_planner(session, pipeline.config),
            pipeline.clock,
        )
        updated = service.update(campaign.id, uuid4(), CampaignCriteria(name="New"))
    with pipeline.sessions() as session:
        current = SqlCampaignRepository(session).get(campaign.id)
        assert current == updated
        assert session.scalar(select(func.count()).select_from(CriteriaRow)) == 2
    assert len(pipeline.jobs()) == 1 and pipeline.jobs()[0].last_error_code == "criteria_superseded"


def test_completion_checks_validity_again_after_matcher_returns(pipeline: Pipeline) -> None:
    pipeline.publish([row("a", deadline="2026-01-01T00:00:05Z")])
    pipeline.campaign()
    lease = pipeline.store.claim(1, pipeline.clock.now())[0]
    inputs = pipeline.store.inputs(lease, pipeline.clock.now())
    assert inputs is not None
    pipeline.clock.instant += timedelta(seconds=6)
    assert not pipeline.store.complete(
        lease,
        MatchResult(result="match", score=1, reasons=(), uncertainties=()),
        pipeline.clock.now(),
    )
    assert not pipeline.results() and pipeline.jobs()[0].last_error_code == "tender_invalid"


def test_attempt_budget_exhausts_on_repeated_matcher_failures(migrated_engine: Engine) -> None:
    pipe = Pipeline(
        migrated_engine, fake_config(retry={"max_attempts": 2, "backoff_seconds": 0.01})
    )
    pipe.publish([row("a")])
    pipe.campaign()
    fake = ScriptedMatcher((TimeoutError(), TimeoutError()))
    worker = pipe.worker(lambda settings: fake, "fake")
    assert worker.run_batch().retried == 1
    pipe.clock.instant += timedelta(seconds=1)
    assert worker.run_batch().failed == 1
    assert worker.run_batch().claimed == 0 and not pipe.results()
    assert pipe.jobs()[0].status == "failed" and pipe.jobs()[0].attempts == 2


def test_stale_criteria_update_and_conflicting_replay_leave_state_unchanged(
    pipeline: Pipeline,
) -> None:
    pipeline.publish([row("a")])
    campaign_id, version_id = uuid4(), uuid4()
    criteria = CampaignCriteria(name="C")
    campaign = pipeline.campaign(criteria, id=campaign_id, version_id=version_id)
    assert pipeline.campaign(criteria, id=campaign_id, version_id=version_id) == campaign
    with pytest.raises(ValueError):
        pipeline.campaign(CampaignCriteria(name="Wrong"), id=campaign_id, version_id=version_id)
    with pipeline.sessions.begin() as session:
        service = CampaignService(
            SqlCampaignRepository(session),
            matching_planner(session, pipeline.config),
            pipeline.clock,
        )
        with pytest.raises(ValueError, match="baseline"):
            service.update(
                campaign_id, uuid4(), CampaignCriteria(name="New"), expected_version_id=uuid4()
            )
    assert len(pipeline.jobs()) == 1


def test_claim_transaction_rollback_does_not_lose_job(pipeline: Pipeline) -> None:
    pipeline.publish([row("a")])
    pipeline.campaign()
    with pytest.raises(RuntimeError), pipeline.sessions.begin() as session:
        assert (
            SqlMatchJobRepository(session, pipeline.clock).claim(pipeline.clock.now(), 20)
            is not None
        )
        raise RuntimeError("crash before claim commit")
    assert pipeline.jobs()[0].status == "pending" and pipeline.jobs()[0].attempts == 0
    assert pipeline.worker().run_batch().completed == 1


def test_model_version_mismatch_does_not_call_wrong_matcher(migrated_engine: Engine) -> None:
    pipe = Pipeline(migrated_engine, fake_config())
    pipe.publish([row("a")])
    pipe.campaign()
    fake = ScriptedMatcher(
        (MatchResult(result="match", score=1, reasons=(), uncertainties=()),), version="wrong"
    )
    assert pipe.worker(lambda settings: fake, "fake").run_batch().retried == 1
    assert not fake.calls and not pipe.results()


def test_lost_completion_acknowledgement_does_not_duplicate_result(pipeline: Pipeline) -> None:
    pipeline.publish([row("a")])
    pipeline.campaign()

    class LostAckStore(TransactionalMatchingStore):
        def complete(self, lease: object, outcome: MatchResult, now: datetime) -> bool:
            super().complete(lease, outcome, now)  # type: ignore[arg-type]
            raise RuntimeError("lost completion acknowledgement after commit")

    registry: Registry[MatcherFactory] = Registry()
    registry.register("rules", lambda settings: RulesMatcher(settings, pipeline.clock))
    with pytest.raises(RuntimeError):
        BatchMatchingWorker(
            LostAckStore(pipeline.sessions, pipeline.config), registry, pipeline.clock
        ).run_batch()
    assert len(pipeline.results()) == 1 and pipeline.jobs()[0].status == "done"
    assert pipeline.worker().run_batch().claimed == 0


def test_intelligent_replay_criteria_and_matcher_versions_preserve_results(
    migrated_engine: Engine,
) -> None:
    from tender_system.core.ports import ModelResponse
    from tender_system.matching.intelligent_matching import IntelligentMatcher
    from tender_system.testing.fakes import ScriptedModelClient

    pipe = Pipeline(migrated_engine, fake_config(matcher_adapter="intelligent"))
    pipe.publish([row("a")])
    campaign = pipe.campaign(CampaignCriteria(name="IT", description="Network equipment"))
    answer = ModelResponse(
        output={
            "result": "match",
            "score": 0.98,
            "confidence": 0.95,
            "reasons": ["Routers satisfy the network equipment request."],
            "uncertainties": [],
            "evidence": [{"field": "title", "quote": "Network routers"}],
        },
        model_version="fake-semantic-v1",
        input_tokens=20,
        cost=0.001,
    )
    client = ScriptedModelClient((answer, answer))
    worker = pipe.worker(
        lambda s: IntelligentMatcher(s, pipe.clock, lambda _: client), "intelligent"
    )
    assert worker.run_batch().completed == 1
    original = pipe.results()[0]
    assert original.outcome.decision_stage == "semantic"
    assert original.outcome.model_calls[0].model_version == "fake-semantic-v1"
    assert worker.run_batch().claimed == 0 and len(client.requests) == 1
    pipe.publish([row("a")])
    assert len(pipe.jobs()) == 1
    updated = pipe.update(
        campaign, CampaignCriteria(name="IT", description="Network infrastructure")
    )
    assert worker.run_batch().completed == 1 and len(client.requests) == 2
    assert len(pipe.results()) == 2 and original in pipe.results()
    assert {r.criteria_version_id for r in pipe.results()} == {
        campaign.current_criteria_version_id,
        updated.current_criteria_version_id,
    }
    values = pipe.config.model_dump(by_alias=True)
    values["global"]["campaign"]["matcher_version"] = "2"
    pipe.config = Configuration.model_validate(values)
    with pipe.sessions.begin() as session:
        CampaignService(
            SqlCampaignRepository(session), matching_planner(session, pipe.config), pipe.clock
        ).rematch(campaign.id)
    hardened_client = ScriptedModelClient(
        (
            ModelResponse(
                output={
                    **answer.output,
                    "field_assessments": [
                        {
                            "field": "title",
                            "quote": "Network routers",
                            "relation": "direct",
                            "detail": "Routers are requested network equipment.",
                        }
                    ],
                    "scope_relation": "direct",
                    "support": "explicit",
                    "evidence_conflicts": False,
                    "criteria_conflicts": False,
                    "critical_missing_information": [],
                    "justification": "clear",
                },
                model_version="fake-semantic-v2",
            ),
        )
    )
    hardened_worker = pipe.worker(
        lambda s: IntelligentMatcher(s, pipe.clock, lambda _: hardened_client), "intelligent"
    )
    assert hardened_worker.run_batch().completed == 1
    assert {r.matcher_version for r in pipe.results()} == {"1", "2"}
    assert original in pipe.results() and len(pipe.results()) == 3
    hardened_result = next(r for r in pipe.results() if r.matcher_version == "2")
    assert hardened_result.outcome.result == "match"
    assert hardened_result.outcome.model_calls[0].model_version == "fake-semantic-v2"
    assert hardened_worker.run_batch().claimed == 0 and len(hardened_client.requests) == 1


def test_unused_semantic_routing_does_not_duplicate_rules_work(pipeline: Pipeline) -> None:
    from tender_system.core.configuration import MatcherRouting

    pipeline.publish([row("a")])
    campaign = pipeline.campaign()
    assert pipeline.worker().run_batch().completed == 1
    old = pipeline.results()[0]
    settings = pipeline.config.global_settings.campaign.model_copy(
        update={
            "routing": MatcherRouting(max_model_calls=0),
        }
    )
    pipeline.config = pipeline.config.model_copy(
        update={
            "global_settings": pipeline.config.global_settings.model_copy(
                update={"campaign": settings}
            ),
        }
    )
    with pipeline.sessions.begin() as session:
        CampaignService(
            SqlCampaignRepository(session),
            matching_planner(session, pipeline.config),
            pipeline.clock,
        ).rematch(campaign.id)
    assert pipeline.worker().run_batch().claimed == 0
    assert pipeline.results() == (old,)


def test_campaign_mode_defaults_are_versioned_and_explicit_criteria_win(
    migrated_engine: Engine,
) -> None:
    pipe = Pipeline(migrated_engine, fake_config(matching_mode="strict"))
    defaulted = pipe.campaign(CampaignCriteria(name="Default"))
    explicit = pipe.campaign(CampaignCriteria(name="Explicit", matching_mode="broad"))
    with pipe.sessions() as session:
        repository = SqlCampaignRepository(session)
        assert (
            repository.criteria(defaulted.current_criteria_version_id).criteria.matching_mode
            == "strict"
        )
        assert (
            repository.criteria(explicit.current_criteria_version_id).criteria.matching_mode
            == "broad"
        )


def test_campaign_batch_override_limits_claim_without_losing_work(migrated_engine: Engine) -> None:
    campaign_id = uuid4()
    config = Configuration.model_validate(
        {
            "global": {
                "source": fixture_source_settings().model_dump(),
                "campaign": {"batch_size": 10, "timeout_seconds": 0.1, "lease_seconds": 20},
            },
            "campaigns": {str(campaign_id): {"batch_size": 1}},
        }
    )
    configuration = Pipeline(migrated_engine, config)
    configuration.campaign(id=campaign_id)
    configuration.publish([row(str(index)) for index in range(3)])
    worker = configuration.worker()
    for _ in range(3):
        report = worker.run_batch(10)
        assert report.claimed == report.completed == 1
    assert worker.run_batch(10).claimed == 0
    assert len(configuration.results()) == 3


@pytest.mark.parametrize(
    "model_output,expected_result,expected_error,expected_detail",
    [
        (
            {
                "result": "match",
                "score": 0.9,
                "confidence": 0.9,
                "reasons": ["The requested cable appears in the product listing."],
                "uncertainties": [],
                "evidence": [{"field": "product_items.0.name", "quote": "Кабель - USB-C"}],
            },
            "match",
            None,
            None,
        ),
        (
            {
                "result": "no_match",
                "score": 0.1,
                "confidence": 0.9,
                "reasons": ["The listed product is outside the campaign scope."],
                "uncertainties": [],
                "evidence": [{"field": "product_items.0.name", "quote": "Кабель - USB-C"}],
            },
            "no_match",
            None,
            None,
        ),
        (
            {
                "result": "unsure",
                "score": 0.5,
                "confidence": 0.4,
                "reasons": ["Product use is unclear."],
                "uncertainties": ["The product use could change relevance."],
                "evidence": [],
            },
            "unsure",
            None,
            None,
        ),
        (
            {
                "result": "match",
                "score": 0.9,
                "confidence": 0.9,
                "reasons": ["The request appears relevant."],
                "uncertainties": [],
                "evidence": [],
            },
            "unsure",
            "invalid_judgment",
            None,
        ),
        (
            {
                "result": "match",
                "score": 0.9,
                "confidence": 0.9,
                "reasons": ["A fabricated item matches."],
                "uncertainties": [],
                "evidence": [{"field": "product_items.0.name", "quote": "Fabricated cable"}],
            },
            "unsure",
            "invalid_evidence",
            "quote_not_found:product_items.0.name",
        ),
        (
            {
                "result": "match",
                "score": "high",
                "confidence": 0.9,
                "reasons": ["Malformed decision."],
                "uncertainties": [],
                "evidence": [{"field": "product_items.0.name", "quote": "Кабель — USB‑C"}],
            },
            "unsure",
            "invalid_judgment",
            None,
        ),
    ],
    ids=(
        "formatted-product-match",
        "grounded-negative",
        "uncertain",
        "missing-evidence",
        "fabricated",
        "malformed",
    ),
)
def test_groq_semantic_evidence_persists_only_validated_decisions(
    migrated_engine: Engine,
    model_output: dict[str, object],
    expected_result: str,
    expected_error: str | None,
    expected_detail: str | None,
) -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen3.8-27b",
                "usage": {"prompt_tokens": 2196, "completion_tokens": 257},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(model_output, ensure_ascii=False)},
                    }
                ],
            },
        )

    config = Configuration.model_validate(
        {
            "global": {
                "source": fixture_source_settings().model_dump(),
                "campaign": {
                    "matcher_adapter": "intelligent",
                    "matcher_version": "1",
                    "batch_size": 10,
                    "timeout_seconds": 1,
                    "lease_seconds": 20,
                    "model": {
                        "provider": "groq",
                        "model": "qwen/qwen3.8-27b",
                        "max_attempts": 1,
                    },
                },
            }
        }
    )
    pipe = Pipeline(migrated_engine, config)
    pipe.publish([row("a", title="Equipment", product_items=[{"name": "Кабель — USB‑C"}])])
    pipe.campaign(CampaignCriteria(name="Cable", include_keywords=("cable",)))
    client = GroqModelClient(SecretStr("offline-test-key"), httpx.MockTransport(handle))
    worker = pipe.worker(
        lambda settings: IntelligentMatcher(settings, pipe.clock, lambda _: client), "intelligent"
    )

    report = worker.run_batch()
    stored = pipe.results()[0]
    assert report.completed == 1 and len(pipe.results()) == 1
    assert len(sent) == 1  # max_attempts=1; test transport never reaches Groq
    assert stored.outcome.result == expected_result
    call = stored.outcome.model_calls[0]
    assert call.attempts == 1
    assert call.error_code == expected_error
    assert call.failure_detail == expected_detail
    if expected_result == "match":
        assert stored.outcome.evidence[0].field == "product_items.0.name"
        assert stored.outcome.evidence[0].quote == "Кабель — USB‑C"
    if expected_error:
        assert stored.outcome.evidence == ()
        assert call.failure_stage == "response_validation"


@pytest.mark.parametrize(
    "model_output,expected_result,verification,verified_count",
    [
        (
            {
                "decision": "match",
                "confidence": 0.95,
                "explanation": "The requested cable appears in the product listing.",
            },
            "unsure",
            "not_provided",
            0,
        ),
        (
            {
                "decision": "no_match",
                "confidence": 0.95,
                "explanation": "The tender requests unrelated equipment.",
            },
            "unsure",
            "not_provided",
            0,
        ),
        (
            {
                "decision": "unsure",
                "confidence": 0.4,
                "explanation": "The product use is unclear.",
            },
            "unsure",
            "not_provided",
            0,
        ),
        (
            {
                "decision": "match",
                "confidence": 0.95,
                "explanation": "The requested cable appears in the product listing.",
                "evidence": [{"field": "product_items.0.name", "quote": "Кабель — USB‑C"}],
            },
            "match",
            "verified",
            1,
        ),
        (
            {
                "decision": "match",
                "confidence": 0.95,
                "explanation": "The model proposes a match, but its citation is invalid.",
                "evidence": [{"field": "unknown_field", "quote": "fabricated"}],
            },
            "unsure",
            "invalid",
            0,
        ),
    ],
    ids=("match-no-evidence", "no-match", "unsure", "verified-evidence", "invalid-evidence"),
)
def test_provider_neutral_decision_and_opinion_persist_in_isolated_postgres(
    migrated_engine: Engine,
    model_output: dict[str, object],
    expected_result: str,
    verification: str,
    verified_count: int,
) -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "model": "fixture-qwen",
                "usage": {"prompt_tokens": 21, "completion_tokens": 8},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(model_output, ensure_ascii=False)},
                    }
                ],
            },
        )

    config = Configuration.model_validate(
        {
            "global": {
                "source": fixture_source_settings().model_dump(),
                "campaign": {
                    "matcher_adapter": "intelligent",
                    "matcher_version": "3",
                    "batch_size": 10,
                    "timeout_seconds": 1,
                    "lease_seconds": 20,
                    "model": {
                        "provider": "groq",
                        "model": "fixture-qwen",
                        "max_attempts": 3,
                    },
                },
            }
        }
    )
    pipe = Pipeline(migrated_engine, config)
    pipe.publish([row("a", title="Equipment", product_items=[{"name": "Кабель — USB‑C"}])])
    pipe.campaign(CampaignCriteria(name="Cable", include_keywords=("cable",)))
    client = GroqModelClient(SecretStr("offline-test-key"), httpx.MockTransport(handle))
    worker = pipe.worker(
        lambda settings: IntelligentMatcher(settings, pipe.clock, lambda _: client), "intelligent"
    )

    assert worker.run_batch().completed == 1
    assert len(sent) == 1
    stored = pipe.results()[0]
    assert stored.matcher_version == "3"
    assert stored.outcome.result == expected_result
    assert stored.outcome.model_opinion is not None
    assert stored.outcome.model_opinion.decision == model_output["decision"]
    assert stored.outcome.evidence_verification is not None
    assert stored.outcome.evidence_verification.status == verification
    if verification == "not_provided" and model_output["decision"] != "unsure":
        assert "evidence_required" in {reason.code for reason in stored.outcome.uncertainties}
    assert len(stored.outcome.evidence) == verified_count
    assert stored.outcome.model_calls[0].error_code is None
    assert stored.outcome.model_calls[0].attempts == 1
    if verification == "invalid":
        assert stored.outcome.model_opinion.decision == "match"
        assert stored.outcome.evidence_verification.error_code == "invalid_evidence"
        assert "model_unavailable" not in {reason.code for reason in stored.outcome.reasons}
