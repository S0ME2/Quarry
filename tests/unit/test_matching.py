from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tender_system.core.configuration import CampaignSettings, Configuration
from tender_system.core.domain import CampaignCriteria, DeadlineRules, MatchResult, Tender
from tender_system.core.ports import Matcher
from tender_system.matching.matching import RulesMatcher, bounded_match, settings_digest
from tender_system.testing.fakes import FakeClock, FunctionMatcher, ScriptedMatcher

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def tender(**values: object) -> Tender:
    return Tender.model_validate(
        dict(
            internal_id=uuid4(),
            current_version_id=uuid4(),
            source_id="s",
            external_tender_id="1",
            title="New network routers",
            description="Switches and routers",
            first_seen_at=NOW,
            last_seen_at=NOW,
            **values,
        )
    )


def test_reference_outcomes_and_interchangeable_matchers() -> None:
    clock = FakeClock(NOW)
    criteria = CampaignCriteria(name="IT", include_keywords=("routers",))
    implementations: tuple[Matcher, ...] = (
        RulesMatcher(CampaignSettings(), clock),
        ScriptedMatcher((MatchResult(result="match", score=1, reasons=(), uncertainties=()),)),
        FunctionMatcher(
            lambda tender, criteria: MatchResult(
                result="match", score=1, reasons=(), uncertainties=()
            )
        ),
    )
    for matcher in implementations:
        assert matcher.match(tender(), criteria).result == "match"


def test_reference_matcher_checks_all_product_names_and_descriptions() -> None:
    item = tender().model_copy(
        update={
            "title": "Office equipment lot",
            "description": None,
            "raw_data": {
                "product_items": [
                    {"Product_Name": "Workstation", "Description": "Business computer"},
                    {"Product_Name": "Laser printer", "Description": "A3 duplex color"},
                ]
            },
        }
    )
    criteria = CampaignCriteria(
        name="Office equipment", include_keywords=("laser printer",), matching_mode="strict"
    )
    result = RulesMatcher(CampaignSettings(), FakeClock(NOW)).match(item, criteria)
    assert result.result == "match"


def test_reference_matcher_reads_snake_case_products_and_excludes_any_product() -> None:
    item = tender().model_copy(
        update={
            "title": "Office equipment lot",
            "description": None,
            "raw_data": {
                "product_items": [
                    {"product_name": "Workstation", "description": "Business computer"},
                    {"product_name": "Laser printer", "description": "A3 duplex color"},
                ]
            },
        }
    )
    matcher = RulesMatcher(CampaignSettings(), FakeClock(NOW))
    assert (
        matcher.match(
            item,
            CampaignCriteria(name="Office equipment", include_keywords=("laser printer",)),
        ).result
        == "match"
    )
    excluded = matcher.match(
        item,
        CampaignCriteria(name="Office equipment", exclude_keywords=("A3 duplex color",)),
    )
    assert excluded.result == "no_match"
    assert any(reason.code == "excluded_keyword" for reason in excluded.reasons)


@pytest.mark.parametrize(
    "criteria,fields,outcome",
    [
        ({"include_keywords": ["routers"]}, {}, "match"),
        ({"include_keywords": ["cement"]}, {}, "no_match"),
        ({"include_keywords": ["routers", "cement"]}, {}, "unsure"),
        ({"include_keywords": ["routers", "cement"], "matching_mode": "broad"}, {}, "match"),
        ({"include_keywords": ["routers", "cement"], "matching_mode": "strict"}, {}, "no_match"),
        ({"exclude_keywords": ["network"]}, {}, "no_match"),
        (
            {"min_value": "10", "max_value": "20", "currencies": ["USD"]},
            {"value": "15", "currency": "USD"},
            "match",
        ),
        ({"min_value": "10", "currencies": ["USD"]}, {"value": "9", "currency": "USD"}, "no_match"),
        ({"min_value": "10"}, {"value": "9"}, "unsure"),
        ({"min_value": "10"}, {}, "unsure"),
        ({"currencies": ["USD"]}, {"currency": "usd"}, "match"),
        ({"currencies": ["USD"]}, {"currency": "UZS"}, "no_match"),
        ({"buyers": ["Buyer"]}, {"buyer": " Buyer "}, "match"),
        ({"locations": ["Tashkent"]}, {"location": "Samarkand"}, "no_match"),
        ({"status_rules": ["open"]}, {"status": "open"}, "match"),
        ({"description": "Highly intelligent semantic intent"}, {}, "unsure"),
        ({"custom_notes": "Unassessed custom preference"}, {}, "unsure"),
    ],
)
def test_reference_criteria_filters(
    criteria: dict[str, object], fields: dict[str, object], outcome: str
) -> None:
    result = RulesMatcher(CampaignSettings(), FakeClock(NOW)).match(
        tender(**fields), CampaignCriteria.model_validate({"name": "IT", **criteria})
    )
    assert result.result == outcome and 0 <= result.score <= 1
    assert result.reasons
    if outcome == "unsure" and result.score == 1:
        assert result.uncertainties


def test_deadline_rules_use_injected_clock_and_missing_evidence() -> None:
    matcher = RulesMatcher(CampaignSettings(), FakeClock(NOW))
    criteria = CampaignCriteria(
        name="Deadline", deadline_rules=DeadlineRules(min_days_remaining=2, max_days_remaining=3)
    )
    assert matcher.match(tender(deadline=NOW + timedelta(days=2)), criteria).result == "match"
    assert matcher.match(tender(deadline=NOW + timedelta(days=1)), criteria).result == "no_match"
    assert matcher.match(tender(), criteria).result == "unsure"


@pytest.mark.parametrize(
    "field",
    ["include_keywords", "exclude_keywords", "currencies", "buyers", "locations", "status_rules"],
)
def test_blank_criterion_is_rejected_at_boundary(field: str) -> None:
    with pytest.raises(ValidationError):
        CampaignCriteria.model_validate({"name": "C", field: [" "]})


def test_criteria_and_nested_deadline_rules_are_immutable() -> None:
    criteria = CampaignCriteria(name="C", min_value=Decimal("10"))
    with pytest.raises(ValidationError):
        criteria.include_keywords = ("changed",)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        criteria.deadline_rules.min_days_remaining = 1  # type: ignore[misc]


def test_scoped_matching_policy_and_invalid_lease() -> None:
    config = Configuration.model_validate(
        {
            "global": {"campaign": {"matcher_version": "2"}},
            "campaigns": {
                "a": {"matcher_adapter": "fake", "timeout_seconds": 1, "lease_seconds": 120}
            },
        }
    )
    assert config.for_campaign("a").matcher_adapter == "fake"
    assert config.for_campaign("b").matcher_adapter == "rules"
    assert config.for_campaign("a").matcher_version == "2"
    assert settings_digest(config.for_campaign("a")) != settings_digest(config.for_campaign("b"))
    with pytest.raises(ValidationError, match="lease"):
        CampaignSettings(timeout_seconds=10, batch_size=100, lease_seconds=1)


def test_hanging_matcher_deadline_returns_without_waiting_for_late_result() -> None:
    from threading import Event

    release = Event()

    def slow(tender: Tender, criteria: CampaignCriteria) -> MatchResult:
        release.wait(timeout=5)
        return MatchResult(result="match", score=1, reasons=(), uncertainties=())

    try:
        with pytest.raises(TimeoutError, match="deadline"):
            bounded_match(FunctionMatcher(slow), tender(), CampaignCriteria(name="C"), 0.01)
    finally:
        release.set()


def test_operational_changes_do_not_duplicate_logical_matching_work() -> None:
    first = CampaignSettings(batch_size=10, timeout_seconds=1, lease_seconds=100)
    second = CampaignSettings(batch_size=20, timeout_seconds=2, lease_seconds=200)
    assert settings_digest(first) == settings_digest(second)
    assert settings_digest(first) != settings_digest(
        first.model_copy(update={"match_threshold": 0.8})
    )


def test_manual_evaluation_id_creates_distinct_job_behavior_identity() -> None:
    settings = CampaignSettings(matcher_adapter="intelligent", matcher_version="1")
    first = settings.model_copy(update={"manual_evaluation_id": uuid4()})
    second = settings.model_copy(update={"manual_evaluation_id": uuid4()})
    assert settings_digest(settings) != settings_digest(first)
    assert settings_digest(first) != settings_digest(second)


def test_reference_version_cannot_be_mislabeled_by_configuration() -> None:
    with pytest.raises(ValueError, match="version 1"):
        RulesMatcher(CampaignSettings(matcher_version="unknown"), FakeClock(NOW))


@pytest.mark.parametrize(
    "criteria,fields,expected",
    [
        ({"exclude_buyers": ["Blocked"]}, {"buyer": " BLOCKED "}, "no_match"),
        ({"exclude_buyers": ["Blocked"]}, {}, "unsure"),
        ({"min_value": 100}, {"value": 1, "currency": "USD"}, "unsure"),
        ({"min_value": 100, "currencies": ["USD"]}, {"value": 1}, "unsure"),
        ({"min_value": 100, "currencies": ["USD"]}, {"currency": "USD"}, "unsure"),
        ({"min_value": 100, "currencies": ["USD"]}, {"value": 1, "currency": "UZS"}, "no_match"),
        ({"max_value": 100, "currencies": ["USD"]}, {"value": 101, "currency": "USD"}, "no_match"),
        ({"locations": ["Tashkent"]}, {"location": "Samarkand"}, "no_match"),
        ({"locations": ["Tashkent"]}, {}, "unsure"),
        ({"status_rules": ["open"]}, {"status": "draft"}, "no_match"),
        ({}, {"status": "closed"}, "no_match"),
        ({}, {"status": "completed"}, "no_match"),
        ({}, {"status": "cancelled"}, "no_match"),
        ({}, {"status": "canceled"}, "no_match"),
        ({}, {"deadline": NOW}, "no_match"),
        ({}, {}, "match"),
        ({"deadline_rules": {"min_days_remaining": 2}}, {}, "unsure"),
        (
            {"deadline_rules": {"max_days_remaining": 2}},
            {"deadline": NOW + timedelta(days=3)},
            "no_match",
        ),
        ({"exclude_keywords": ["route"]}, {}, "match"),
        ({"buyers": ["Blocked"], "exclude_buyers": ["Blocked"]}, {"buyer": "Blocked"}, "unsure"),
    ],
)
def test_rules_and_v3_share_hard_restrictions(
    criteria: dict[str, object], fields: dict[str, object], expected: str
) -> None:
    from tender_system.matching.intelligent_matching import IntelligentMatcher
    from tender_system.testing.fakes import ScriptedModelClient

    client = ScriptedModelClient(())
    settings = CampaignSettings(matcher_adapter="intelligent", matcher_version="3")
    campaign = CampaignCriteria.model_validate({"name": "Restrictions", **criteria})
    item = tender(**fields)
    rules = RulesMatcher(CampaignSettings(), FakeClock(NOW)).match(item, campaign)
    ai = IntelligentMatcher(settings, FakeClock(NOW), lambda _: client).match(item, campaign)
    assert rules.result == ai.result == expected
    assert not client.requests
    if fields.get("currency") == "UZS":
        assert not any(r.code == "value_below_minimum" for r in ai.reasons)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reasoning_effort", "high"),
        ("max_output_tokens", 100),
        ("structured_output", "schema"),
        ("model", "changed"),
    ],
)
def test_model_behavior_invalidates_ai_cache_only(field: str, value: object) -> None:
    settings = CampaignSettings(matcher_adapter="intelligent", matcher_version="3")
    changed = settings.model_copy(
        update={"model": settings.model.model_copy(update={field: value})}
    )
    assert settings_digest(settings) != settings_digest(changed)
    assert settings_digest(
        settings.model_copy(update={"matcher_adapter": "rules"})
    ) == settings_digest(changed.model_copy(update={"matcher_adapter": "rules"}))


def test_deadline_context_changes_only_at_restriction_boundaries() -> None:
    from tender_system.matching.matching import evaluation_context

    item = tender(deadline=NOW + timedelta(days=5))
    criteria = CampaignCriteria(
        name="Window", deadline_rules=DeadlineRules(min_days_remaining=1, max_days_remaining=3)
    )
    settings = CampaignSettings()

    def context(at: datetime) -> str:
        return evaluation_context(item, criteria, settings, at)

    assert context(NOW) == context(NOW + timedelta(days=1))
    assert context(NOW) != context(NOW + timedelta(days=2))
    assert context(NOW + timedelta(days=2)) == context(NOW + timedelta(days=4))
    assert context(NOW + timedelta(days=4)) != context(NOW + timedelta(days=4, seconds=1))
    assert context(NOW + timedelta(days=4, seconds=1)) != context(NOW + timedelta(days=5))


def test_legacy_raw_product_changes_invalidate_context_but_unrelated_raw_fields_do_not() -> None:
    from tender_system.matching.matching import evaluation_context

    settings = CampaignSettings(matcher_adapter="intelligent", matcher_version="3")
    criteria = CampaignCriteria(name="Network", include_keywords=("router",))
    item = tender(raw_data={"product_items": [{"name": "Router"}], "nonce": 1})
    changed_nonce = item.model_copy(update={"raw_data": {**item.raw_data, "nonce": 2}})
    changed_product = item.model_copy(update={"raw_data": {"product_items": [{"name": "Cement"}]}})
    context = evaluation_context(item, criteria, settings, NOW)
    assert context == evaluation_context(changed_nonce, criteria, settings, NOW)
    assert context != evaluation_context(changed_product, criteria, settings, NOW)


@pytest.mark.parametrize(
    "field,value",
    [("source_url", "https://example.test/1"), ("document_urls", ("https://example.test/doc",))],
)
def test_explicit_required_metadata_invalidates_context(field: str, value: object) -> None:
    from tender_system.core.configuration import UncertaintySettings
    from tender_system.matching.matching import evaluation_context

    settings = CampaignSettings(uncertainty=UncertaintySettings(required_fields=(field,)))
    criteria = CampaignCriteria(name="Required metadata")
    item = tender()
    changed = item.model_copy(update={field: value})
    assert evaluation_context(item, criteria, settings, NOW) != evaluation_context(
        changed, criteria, settings, NOW
    )


@pytest.mark.parametrize("mode", ["broad", "balanced", "strict"])
def test_missing_subject_stays_uncertain_with_required_keywords(mode: str) -> None:
    item = tender().model_copy(update={"title": None, "description": None})
    criteria = CampaignCriteria.model_validate(
        {"name": "Network", "include_keywords": ["router"], "matching_mode": mode}
    )
    result = RulesMatcher(CampaignSettings(), FakeClock(NOW)).match(item, criteria)
    assert result.result == "unsure"
    assert any(reason.code == "missing_content" for reason in result.uncertainties)


@pytest.mark.parametrize(
    "incomplete, criteria_values, expected",
    [
        (False, {}, "match"),
        (True, {}, "unsure"),
        (True, {"exclude_keywords": ["routers"]}, "no_match"),
        (True, {"include_keywords": ["photocopier"]}, "unsure"),
    ],
)
def test_worker_obeys_replaceable_store_port_without_database(
    incomplete: bool, criteria_values: dict[str, object], expected: str
) -> None:
    from typing import Literal

    from tender_system.core.domain import CriteriaVersion, JobLease, MatchingInput, MatchJob
    from tender_system.core.ports import MatcherFactory, MatchingWorkStore
    from tender_system.core.registry import Registry
    from tender_system.matching.matching import BatchMatchingWorker

    clock = FakeClock(NOW)
    item = tender().model_copy(update={"documents_incomplete": incomplete})
    criteria = CriteriaVersion(
        id=uuid4(),
        campaign_id=uuid4(),
        criteria=CampaignCriteria.model_validate({"name": "C", **criteria_values}),
        created_at=NOW,
    )
    job = MatchJob(
        id=uuid4(),
        tender_id=item.internal_id,
        tender_version_id=item.current_version_id,
        campaign_id=criteria.campaign_id,
        criteria_version_id=criteria.id,
        matcher_version="1",
        matcher_config=CampaignSettings().model_dump(mode="json"),
        created_at=NOW,
    )
    lease = JobLease(job=job, token=uuid4(), expires_at=NOW + timedelta(hours=1))

    class Store:
        outcome: MatchResult | None = None

        def claim(self, limit: int, now: datetime) -> tuple[JobLease, ...]:
            return (lease,)

        def inputs(self, lease: JobLease, now: datetime) -> MatchingInput | None:
            return MatchingInput(tender=item, criteria=criteria)

        def complete(self, lease: JobLease, outcome: MatchResult, now: datetime) -> bool:
            self.outcome = outcome
            return True

        def fail(
            self, lease: JobLease, error_code: str, now: datetime
        ) -> Literal["retry", "failed", "lost"]:
            raise AssertionError("unexpected failure")

    store = Store()
    port: MatchingWorkStore = store
    registry: Registry[MatcherFactory] = Registry()
    registry.register("rules", lambda settings: RulesMatcher(settings, clock))
    assert BatchMatchingWorker(port, registry, clock).run_batch().completed == 1
    assert store.outcome is not None and store.outcome.criteria_version_id == criteria.id

    assert store.outcome.result == expected


def test_included_keyword_takes_priority_over_stop_words() -> None:
    matcher = RulesMatcher(CampaignSettings(), FakeClock(NOW))
    item = tender()
    criteria = CampaignCriteria(
        name="IT", include_keywords=("routers",), exclude_keywords=("switches", "routers")
    )
    assert matcher.match(item, criteria).result == "match"
    excluded = CampaignCriteria(
        name="IT", include_keywords=("printers",), exclude_keywords=("switches",)
    )
    assert matcher.match(item, excluded).result == "no_match"


def test_document_text_participates_in_rules_and_fingerprint() -> None:
    from tender_system.matching.matching import evaluation_context

    item = tender().model_copy(update={"document_text": "Supply photocopiers"})
    criteria = CampaignCriteria(name="Office", include_keywords=("photocopiers",))
    settings = CampaignSettings()
    assert RulesMatcher(settings, FakeClock(NOW)).match(item, criteria).result == "match"
    assert evaluation_context(item, criteria, settings, NOW) != evaluation_context(
        item.model_copy(update={"document_text": "Supply scanners"}), criteria, settings, NOW
    )
