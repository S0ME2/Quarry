import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import cast
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from tender_system.core.configuration import (
    CampaignSettings,
    Configuration,
    MatcherRouting,
    ModelSettings,
    ModeThresholds,
    ThresholdPolicy,
)
from tender_system.core.domain import CampaignCriteria, Tender
from tender_system.core.ports import ModelFailure, ModelRequest, ModelResponse
from tender_system.matching.calibrated_semantics import CalibratedSemanticMatcher
from tender_system.matching.intelligent_matching import IntelligentMatcher
from tender_system.matching.matcher_evaluation import (
    Benchmark,
    ReplayClient,
    evaluate,
    load_env,
    metrics,
)
from tender_system.matching.matching import settings_digest
from tender_system.matching.model_providers import (
    GroqModelClient,
    OpenRouterModelClient,
    model_client,
)
from tender_system.matching.semantic_matching import SemanticMatcher
from tender_system.testing.fakes import FakeClock, FunctionModelClient, ScriptedModelClient

NOW = datetime(2026, 10, 6, tzinfo=UTC)


def test_semantic_request_excludes_private_notification_destination() -> None:
    criteria = CampaignCriteria.model_validate(
        {
            "name": "Network",
            "notifications": {"chat_id": "123456789"},
            "include_keywords": ["router"],
        }
    )
    request = SemanticMatcher().request(tender(), criteria, ModelSettings(model="fake"), 10)
    campaign = request.payload["campaign"]
    assert isinstance(campaign, dict)
    assert "notifications" not in campaign
    assert "123456789" not in json.dumps(request.payload)


def tender(**changes: object) -> Tender:
    return Tender.model_validate(
        {
            "source_id": "test",
            "external_tender_id": "1",
            "internal_id": uuid4(),
            "current_version_id": uuid4(),
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "title": "Ethernet switches",
            **changes,
        }
    )


def response(**changes: object) -> ModelResponse:
    output = {
        "result": "match",
        "score": 0.97,
        "confidence": 0.95,
        "reasons": ["Ethernet switches satisfy the requested network equipment scope."],
        "uncertainties": [],
        "evidence": [{"field": "title", "quote": "Ethernet switches"}],
        **changes,
    }
    return ModelResponse(
        output=cast(dict[str, JsonValue], output),
        model_version="fake-v1",
        input_tokens=42,
        output_tokens=30,
        cost=0.001,
    )


def test_requirements_conditions_reach_model_rules_and_fingerprint() -> None:
    from tender_system.matching.matcher_rules import RuleEvaluator, keyword_found
    from tender_system.matching.semantic_matching import tender_fields

    item = tender(
        title="Procurement",
        raw_data={
            "requirements": "Supply Ethernet switches with installation",
            "conditions": "Delivery within 30 days; no civil works",
            "private_metadata": "Must not enter model input",
        },
    )
    criteria = CampaignCriteria(
        name="Network", include_keywords=("Ethernet switches",), exclude_keywords=("civil works",)
    )
    fields = tender_fields(item)
    assert fields["requirements"] == item.raw_data["requirements"]
    assert fields["conditions"] == item.raw_data["conditions"]
    assert "private_metadata" not in fields
    assert keyword_found(item, criteria)
    assert not RuleEvaluator().evaluate(item, criteria, NOW, MatcherRouting()).failures
    request = SemanticMatcher().request(item, criteria, ModelSettings(model="fake"), 5)
    supplied = request.payload["tender"]
    assert isinstance(supplied, dict) and supplied["conditions"] == fields["conditions"]
    accepted = SemanticMatcher().validate(
        response(
            evidence=[
                {
                    "field": "requirements",
                    "quote": "Ethernet switches",
                }
            ]
        ),
        item,
        request,
    )
    assert accepted.evidence[0].field == "requirements"
    excluded = RuleEvaluator().evaluate(
        item,
        CampaignCriteria(name="Other", exclude_keywords=("civil works",)),
        NOW,
        MatcherRouting(),
    )
    assert any(reason.code == "excluded_keyword" for reason in excluded.failures)
    from tender_system.matching.matching import evaluation_context

    settings = CampaignSettings()
    before = evaluation_context(item, criteria, settings, NOW)
    changed = item.model_copy(
        update={"raw_data": {**item.raw_data, "conditions": "Delivery in 90 days"}}
    )
    assert evaluation_context(changed, criteria, settings, NOW) != before


def test_semantic_matcher_receives_canonical_products_and_accepts_path_evidence() -> None:
    products = [
        {"Product_Name": "Workstation", "Description": "32 GB RAM", "Quantity": 12},
        {"Product_Name": "Laser printer", "Description": "A3 duplex color", "Quantity": 3},
    ]
    item = tender(raw_data={"product_items": products})
    request = SemanticMatcher().request(
        item, CampaignCriteria(name="C"), ModelSettings(model="fake"), 5
    )
    tender_payload = request.payload["tender"]
    assert isinstance(tender_payload, dict)
    products_payload = tender_payload["product_candidates"]
    assert isinstance(products_payload, list) and len(products_payload) == 2
    normalized = cast(list[dict[str, JsonValue]], products_payload)
    assert normalized[0]["name"] == "Workstation"
    assert normalized[1]["description"] == "A3 duplex color"
    assert normalized[1]["attributes"] == products[1]

    accepted = SemanticMatcher().validate(
        response(evidence=[{"field": "product_items.1.description", "quote": "A3 duplex color"}]),
        item,
        request,
    )
    assert accepted.evidence[0].field == "product_items.1.description"


def test_large_tender_keeps_product_312_and_incomplete_negative_abstains() -> None:
    products = [
        {"id": f"D-{index}", "Product_Name": "Office chair", "Description": "Ergonomic chair"}
        for index in range(1, 312)
    ]
    products.append(
        {
            "id": "INS-312",
            "Product_Name": "Insulin cartridges",
            "Description": "Insulin injection cartridges, 100 IU/mL",
        }
    )
    item = tender(title="Medical supplies", raw_data={"product_items": products})
    criteria = CampaignCriteria(name="Insulin", description="Insulin injection cartridges")
    request = SemanticMatcher().request(item, criteria, ModelSettings(model="fake"), 5)
    tender_payload = request.payload["tender"]
    selection = request.payload["product_selection"]
    assert isinstance(tender_payload, dict) and isinstance(selection, dict)
    candidates = cast(list[dict[str, JsonValue]], tender_payload["product_candidates"])
    assert len(candidates) <= 18
    assert selection == {"complete": False, "included": len(candidates), "total": 312}
    assert any(
        candidate["source_index"] == 311 and candidate["source_product_id"] == "INS-312"
        for candidate in candidates
    )

    client = ScriptedModelClient(
        (
            response(
                result="no_match",
                score=0.05,
                evidence=[
                    {
                        "field": "product_items.311.description",
                        "quote": "Insulin injection cartridges",
                    }
                ],
            ),
        )
    )
    result = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(item, criteria)
    assert result.result == "unsure"
    assert any(
        reason.code == "unexamined_products" and "18 of 312" in reason.detail
        for reason in result.uncertainties
    )
    assert result.model_calls[0].input_tokens == 42


def test_semantic_evidence_cannot_cite_an_omitted_product() -> None:
    products = [
        {"id": f"D-{index}", "Product_Name": "Office chair", "Description": "Ergonomic chair"}
        for index in range(1, 30)
    ]
    item = tender(raw_data={"product_items": products})
    request = SemanticMatcher().request(
        item, CampaignCriteria(name="Insulin"), ModelSettings(model="fake"), 5
    )
    with pytest.raises(ModelFailure, match="invalid_evidence"):
        SemanticMatcher().validate(
            response(
                evidence=[{"field": "product_items.28.description", "quote": "Ergonomic chair"}]
            ),
            item,
            request,
        )


@pytest.mark.parametrize("matcher_type", [SemanticMatcher, CalibratedSemanticMatcher])
def test_canonical_only_products_obey_semantic_context_budget(
    matcher_type: type[SemanticMatcher] | type[CalibratedSemanticMatcher],
) -> None:
    item = tender(
        product_items=[{"name": f"Office chair {index}"} for index in range(40)],
        raw_data={},
    )
    request = matcher_type().request(
        item, CampaignCriteria(name="Office furniture"), ModelSettings(model="fake"), 5
    )
    fields = request.payload["tender"]
    assert isinstance(fields, dict)
    assert "product_items" not in fields
    candidates = fields["product_candidates"]
    assert isinstance(candidates, list) and 0 < len(candidates) <= 18
    assert request.payload["product_selection"] == {
        "complete": False,
        "included": len(candidates),
        "total": 40,
    }


@pytest.mark.parametrize(
    ("product", "expected_name", "expected_description"),
    [
        (
            {"product_name": "Snake product", "description": "Snake description"},
            "Snake product",
            "Snake description",
        ),
        (
            {"Product_Name": "Legacy product", "Description": "Legacy description"},
            "Legacy product",
            "Legacy description",
        ),
        (
            {"ProductName": "Camel product", "productDescription": "Camel description"},
            "Camel product",
            "Camel description",
        ),
        (
            {"name": "Generic product", "item_description": "Generic description"},
            "Generic product",
            "Generic description",
        ),
    ],
)
def test_semantic_normalization_accepts_product_field_variants(
    product: dict[str, object], expected_name: str, expected_description: str
) -> None:
    item = tender(raw_data={"product_items": [product]})
    tender_payload = (
        SemanticMatcher()
        .request(item, CampaignCriteria(name="C"), ModelSettings(model="fake"), 5)
        .payload["tender"]
    )
    assert isinstance(tender_payload, dict)
    products_payload = tender_payload["product_candidates"]
    assert isinstance(products_payload, list)
    normalized = cast(list[dict[str, JsonValue]], products_payload)
    assert normalized[0]["name"] == expected_name
    assert normalized[0]["description"] == expected_description
    assert normalized[0]["attributes"] == product


def test_semantic_normalization_preserves_products_with_missing_description_and_all_fields() -> (
    None
):
    item = tender(
        raw_data={
            "product_items": [
                {
                    "product_name": "Named product",
                    "description": None,
                    "quantity": 4,
                    "custom_code": "X",
                },
                {"custom_payload": {"value": 7}},
            ]
        }
    )
    fields = (
        SemanticMatcher()
        .request(item, CampaignCriteria(name="C"), ModelSettings(model="fake"), 5)
        .payload["tender"]
    )
    assert isinstance(fields, dict)
    products_payload = fields["product_candidates"]
    assert isinstance(products_payload, list) and len(products_payload) == 2
    products = cast(list[dict[str, JsonValue]], products_payload)
    assert products[0]["name"] == "Named product" and products[0]["description"] is None
    assert products[0]["attributes"] == {
        "product_name": "Named product",
        "description": None,
        "quantity": 4,
        "custom_code": "X",
    }
    assert products[1]["attributes"] == {"custom_payload": {"value": 7}}


def test_excluded_product_description_is_checked_before_model_call() -> None:
    client = ScriptedModelClient(())
    item = tender(
        title="General office supplies",
        description=None,
        raw_data={"product_items": [{"product_name": "Printer", "description": "laser printer"}]},
    )
    result = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        item, CampaignCriteria(name="C", exclude_keywords=("laser printer",))
    )
    assert result.result == "no_match" and not client.requests
    assert any(reason.field == "product_items.0.description" for reason in result.reasons)


def test_semantic_hard_exclusion_uses_product_name_before_provider_call() -> None:
    client = ScriptedModelClient(())
    item = tender(raw_data={"product_items": [{"product_name": "Excluded motor"}]})
    result = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        item, CampaignCriteria(name="C", exclude_keywords=("excluded motor",))
    )
    assert result.result == "no_match" and not client.requests
    assert result.reasons[0].field == "product_items.0.name"


def settings(**changes: object) -> CampaignSettings:
    return CampaignSettings.model_validate({"matcher_adapter": "intelligent", **changes})


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_incomplete_documents_abstain_without_provider_configuration(version: str) -> None:
    def unavailable(_: ModelSettings) -> ScriptedModelClient:
        pytest.fail("Incomplete documents must not configure or call a paid provider")

    outcome = IntelligentMatcher(
        settings(matcher_version=version), FakeClock(NOW), unavailable
    ).match(
        tender(documents_incomplete=True, document_text="Partial attachment text"),
        CampaignCriteria(name="IT", description="Network equipment"),
    )
    assert outcome.processing_status == "needs_review"
    assert outcome.relevance is None
    assert outcome.model_calls == ()
    assert "document_processing_incomplete" in {r.code for r in outcome.uncertainties}


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_incomplete_documents_preserve_independent_hard_rejection(version: str) -> None:
    client = ScriptedModelClient(())
    outcome = IntelligentMatcher(
        settings(matcher_version=version), FakeClock(NOW), lambda _: client
    ).match(
        tender(documents_incomplete=True, buyer="Excluded buyer"),
        CampaignCriteria(name="IT", exclude_buyers=("Excluded buyer",)),
    )
    assert outcome.relevance == "NOT_RELEVANT"
    assert not outcome.model_calls and not client.requests


def test_labeled_benchmark_all_modes_and_routing() -> None:
    benchmark = Benchmark.model_validate_json(
        Path("benchmarks/matcher/matcher_v1.json").read_text()
    )
    replay = json.loads(Path("benchmarks/matcher/matcher_replay_v1.json").read_text())
    report = evaluate(
        benchmark, settings(), {key: ReplayClient(value) for key, value in replay.items()}
    )
    aggregate = cast(dict[str, object], report["aggregate"])
    assert aggregate["cases"] == 69
    assert aggregate["decision_accuracy"] == 1
    assert aggregate["false_positives"] == aggregate["false_negatives"] == 0
    assert aggregate["model_errors"] == 0
    assert aggregate["model_calls_per_100_cases"] == pytest.approx(36 / 69 * 100)


def test_live_evaluation_paces_only_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from tender_system.matching import matcher_evaluation as evaluation

    clock = [0.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += seconds

    benchmark = Benchmark.model_validate_json(
        Path("benchmarks/matcher/matcher_v1.json").read_text()
    )
    benchmark = benchmark.model_copy(update={"cases": benchmark.cases[:1]})
    answer = response(evidence=[{"field": "title", "quote": "Network routers"}])
    client = ScriptedModelClient((answer, answer, answer))
    monkeypatch.setattr(evaluation, "model_client", lambda _: client)
    monkeypatch.setattr(evaluation, "monotonic", lambda: clock[0])
    monkeypatch.setattr(evaluation, "sleep", sleep)
    report = evaluate(benchmark, settings(), request_interval=2)
    assert waits == [2, 2] and len(client.requests) == 3
    assert cast(dict[str, object], report["aggregate"])["model_errors"] == 0
    with pytest.raises(ValueError):
        evaluate(benchmark, settings(), workers=2, request_interval=2)


@pytest.mark.parametrize(
    "criteria,fields,code",
    [
        ({}, {"deadline": NOW}, "expired"),
        ({}, {"status": " CANCELLED "}, "inactive_status"),
        (
            {"min_value": "10", "currencies": ["USD"]},
            {"value": "9", "currency": "USD"},
            "value_below_minimum",
        ),
        (
            {"max_value": "10", "currencies": ["USD"]},
            {"value": "11", "currency": "USD"},
            "value_above_maximum",
        ),
        ({"currencies": ["USD"]}, {"currency": "UZS"}, "criterion_failed"),
        ({"buyers": ["City IT"]}, {"buyer": "Other"}, "criterion_failed"),
        ({"exclude_buyers": ["Other"]}, {"buyer": "Other"}, "excluded_buyer"),
        ({"locations": ["Tashkent"]}, {"location": "Samarkand"}, "criterion_failed"),
        ({"status_rules": ["open"]}, {"status": "draft"}, "criterion_failed"),
        (
            {"deadline_rules": {"min_days_remaining": 3}},
            {"deadline": NOW + timedelta(days=2)},
            "deadline_outside_window",
        ),
        ({"buyers": ["Other"], "exclude_buyers": ["Other"]}, {}, "conflicting_criteria"),
        ({"status_rules": ["closed"]}, {}, "conflicting_criteria"),
        ({"exclude_keywords": ["Ethernet"]}, {}, "excluded_keyword"),
    ],
)
def test_rules_reject_without_model(
    criteria: dict[str, object], fields: dict[str, object], code: str
) -> None:
    client = ScriptedModelClient(())
    outcome = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        tender(**fields),
        CampaignCriteria.model_validate(
            {"name": "IT", "description": "Network equipment", **criteria}
        ),
    )
    assert outcome.result == "no_match" and outcome.decision_stage == "rules"
    assert code in {r.code for r in outcome.reasons}
    assert not client.requests and not outcome.model_calls


@pytest.mark.parametrize(
    "criteria",
    [
        {"locations": ["Tashkent"]},
        {"buyers": ["City IT"]},
        {"exclude_buyers": ["Other"]},
        {"currencies": ["USD"]},
        {"min_value": "10", "currencies": ["USD"]},
        {"deadline_rules": {"min_days_remaining": 3}},
    ],
)
def test_missing_required_fields_abstain_before_ai(criteria: dict[str, object]) -> None:
    client = ScriptedModelClient(())
    outcome = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        tender(), CampaignCriteria.model_validate({"name": "IT", **criteria})
    )
    assert outcome.result == "unsure" and outcome.uncertainties and not client.requests


def test_money_units_must_be_known_before_rejection() -> None:
    matcher = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: ScriptedModelClient(()))
    for criteria in (
        CampaignCriteria(name="C", min_value=Decimal("100")),
        CampaignCriteria(name="C", min_value=Decimal("100"), currencies=("USD",)),
    ):
        assert matcher.match(tender(value="9"), criteria).result == "unsure"


def test_keyword_miss_reaches_semantics_and_prompt_excludes_raw_secrets() -> None:
    client = ScriptedModelClient((response(),))
    item = tender(raw_data={"secret": "private"}, source_url="https://private.invalid")
    outcome = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        item,
        CampaignCriteria(name="IT", description="Network equipment", include_keywords=("routers",)),
    )
    assert outcome.result == "match" and outcome.decision_stage == "semantic"
    assert outcome.tender_version_id == item.current_version_id
    assert outcome.matcher_implementation == "intelligent" and outcome.matcher_version == "1"
    assert outcome.model_calls[0].input_tokens == 42 and outcome.model_calls[0].cost == 0.001
    assert "private" not in json.dumps(dict(client.requests[0].payload))
    assert "matching_mode" not in client.requests[0].payload["campaign"]  # type: ignore[operator]


@pytest.mark.parametrize(
    "changes",
    [
        {"score": float("nan")},
        {"confidence": 1.1},
        {"reasons": []},
        {"result": "unsure", "uncertainties": []},
        {"evidence": []},
        {"evidence": [{"field": "location", "quote": "Tashkent"}]},
        {"evidence": [{"field": "invented", "quote": "Ethernet switches"}]},
        {"score": 0.1},
        {"result": "bad"},
        {"extra": "untrusted"},
    ],
)
def test_malformed_or_ungrounded_model_output_never_confident(changes: dict[str, object]) -> None:
    client = ScriptedModelClient((response(**changes),))
    outcome = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        tender(), CampaignCriteria(name="IT", description="Network equipment")
    )
    assert outcome.result == "unsure" and outcome.confidence == 0
    assert outcome.model_calls[0].error_code
    assert outcome.model_calls[0].input_tokens == 42


@pytest.mark.parametrize("failure", [TimeoutError("secret"), RuntimeError("secret")])
def test_model_failure_is_safe_unsure(failure: Exception) -> None:
    client = ScriptedModelClient((failure,))
    result = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        tender(), CampaignCriteria(name="IT", description="Network equipment")
    )
    assert result.result == "unsure" and result.model_calls[0].error_code == type(failure).__name__
    assert result.model_calls[0].failure_stage == "provider_call"
    assert "secret" not in result.model_dump_json()


def test_provider_factory_failure_is_reported_before_transport() -> None:
    def broken_factory(_: ModelSettings) -> object:
        raise ValueError("private configuration detail")

    result = IntelligentMatcher(
        settings(),
        FakeClock(NOW),
        broken_factory,  # type: ignore[arg-type]
    ).match(tender(), CampaignCriteria(name="IT", description="Network equipment"))
    call = result.model_calls[0]
    assert result.result == "unsure"
    assert call.error_code == "ValueError" and call.failure_stage == "provider_configuration"
    assert "private" not in result.model_dump_json()


def test_final_hard_filter_rechecks_deadline_after_semantic_call() -> None:
    clock = FakeClock(NOW)

    def answer(_: ModelRequest) -> ModelResponse:
        clock.instant = NOW + timedelta(days=1)
        return response()

    item = tender(deadline=NOW + timedelta(minutes=1))
    result = IntelligentMatcher(settings(), clock, lambda _: FunctionModelClient(answer)).match(
        item, CampaignCriteria(name="IT", description="Network equipment")
    )
    assert result.result == "no_match" and result.decision_stage == "rules"
    assert result.model_calls and any(reason.code == "expired" for reason in result.reasons)


def test_actual_model_timeout_discards_late_reply() -> None:
    release = Event()

    def slow(request: ModelRequest) -> ModelResponse:
        release.wait(5)
        return response()

    try:
        client = FunctionModelClient(slow)
        result = IntelligentMatcher(
            settings(timeout_seconds=0.03), FakeClock(NOW), lambda _: client
        ).match(tender(), CampaignCriteria(name="IT", description="Network equipment"))
        assert result.result == "unsure" and result.model_calls[0].error_code == "TimeoutError"
    finally:
        release.set()


def test_stronger_provider_escalation_and_metadata() -> None:
    weak = ScriptedModelClient((response(score=0.6),))
    strong = ScriptedModelClient((response(),))
    config = settings(
        routing={
            "escalation_enabled": True,
            "stronger_model": {"provider": "other", "model": "strong"},
        }
    )
    result = IntelligentMatcher(
        config, FakeClock(NOW), lambda m: strong if m.provider == "other" else weak
    ).match(tender(), CampaignCriteria(name="IT", description="Network equipment"))
    assert result.result == "match" and result.decision_stage == "escalation"
    assert len(result.model_calls) == 2 and result.model_calls[1].provider == "other"
    assert strong.requests[0].model == "strong" and len(weak.requests) == 1


def test_stronger_failure_preserves_both_attempts_and_abstains() -> None:
    client = ScriptedModelClient((response(score=0.6), RuntimeError()))
    config = settings(
        routing={
            "escalation_enabled": True,
            "stronger_model": {"provider": "fake", "model": "strong"},
        }
    )
    result = IntelligentMatcher(config, FakeClock(NOW), lambda _: client).match(
        tender(), CampaignCriteria(name="IT", description="Network equipment")
    )
    assert result.result == "unsure" and len(result.model_calls) == 2
    assert result.model_calls[0].model_version == "fake-v1"


@pytest.mark.parametrize(
    "changes",
    [
        {"routing": {"semantic_enabled": False}},
        {"routing": {"max_model_calls": 0}},
        {"budget_limit": 0},
    ],
)
def test_cost_controls_stop_ai(changes: dict[str, object]) -> None:
    client = ScriptedModelClient(())
    result = IntelligentMatcher(settings(**changes), FakeClock(NOW), lambda _: client).match(
        tender(), CampaignCriteria(name="IT", description="Network equipment")
    )
    assert result.result == "unsure" and not client.requests


@pytest.mark.parametrize("limit,budget", [(1, 10), (2, 0.0001)])
def test_escalation_call_and_known_cost_limits(limit: int, budget: float) -> None:
    client = ScriptedModelClient((response(score=0.6),))
    config = settings(
        budget_limit=budget,
        routing={
            "escalation_enabled": True,
            "max_model_calls": limit,
            "stronger_model": {"provider": "fake", "model": "strong"},
        },
    )
    result = IntelligentMatcher(config, FakeClock(NOW), lambda _: client).match(
        tender(), CampaignCriteria(name="IT", description="Network equipment")
    )
    assert result.result == "unsure" and len(client.requests) == 1


def test_keyword_word_boundaries_and_empty_subject() -> None:
    matcher = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: ScriptedModelClient(()))
    assert (
        matcher.match(
            tender(title="Network switches"), CampaignCriteria(name="C", exclude_keywords=("work",))
        ).result
        == "match"
    )
    assert (
        matcher.match(tender(title=None), CampaignCriteria(name="C", description="Networks")).result
        == "unsure"
    )


def test_threshold_override_and_provenance_hash() -> None:
    first = settings()
    second = settings(thresholds=ThresholdPolicy(balanced=ModeThresholds(match=0.6)))
    assert settings_digest(first) != settings_digest(second)
    assert settings_digest(first) != settings_digest(
        settings(routing=MatcherRouting(max_model_calls=0))
    )
    assert settings_digest(first) == settings_digest(
        settings(
            model=first.model.model_copy(update={"api_key_env": "OTHER", "timeout_seconds": 2})
        )
    )
    config = Configuration.model_validate(
        {
            "campaigns": {
                "a": {"routing": {"max_model_calls": 0}, "thresholds": {"strict": {"match": 0.99}}}
            }
        }
    )
    assert config.for_campaign("a").routing.max_model_calls == 0
    assert config.for_campaign("a").thresholds.strict.match == 0.99
    with pytest.raises(ValidationError):
        MatcherRouting(escalation_enabled=True)
    with pytest.raises(ValueError, match="version 1"):
        IntelligentMatcher(
            settings(matcher_version="unsupported"),
            FakeClock(NOW),
            lambda _: ScriptedModelClient(()),
        )


def test_optional_metadata_does_not_prevent_decision() -> None:
    supplied = response()
    client = ScriptedModelClient((ModelResponse(output=supplied.output, model_version="fake"),))
    result = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        tender(), CampaignCriteria(name="C", description="Networks")
    )
    assert result.result == "match" and result.model_calls[0].cost is None


def test_openrouter_alias_payload_and_metadata_without_network() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "stealth/space-bunny-alpha"
        assert body["max_tokens"] == 600
        assert request.headers["Authorization"] == "Bearer fake-test-key"
        return httpx.Response(
            200,
            json={
                "model": "resolved-model",
                "system_fingerprint": "fp1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(dict(response().output))},
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 30, "cost": 0.001},
            },
        )

    client = OpenRouterModelClient(SecretStr("fake-test-key"), httpx.MockTransport(handle))
    answer = client.generate(
        SemanticMatcher().request(
            tender(), CampaignCriteria(name="C"), ModelSettings(model="space-bunny-alpha"), 1
        )
    )
    assert answer.model_version == "resolved-model/fp1" and answer.cost == 0.001


def test_groq_default_routing_and_json_without_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    config = settings().model
    assert (config.provider, config.model, config.credential_env) == (
        "groq",
        "qwen/qwen3.8-27b",
        "GROQ_API_KEY",
    )
    monkeypatch.setenv("GROQ_API_KEY", "fake-test-key")
    assert isinstance(model_client(config), GroqModelClient)

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == config.model
        assert body["reasoning_effort"] == "none"
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "model": config.model,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(dict(response().output)),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 30},
            },
        )

    client = GroqModelClient(SecretStr("fake-test-key"), httpx.MockTransport(handle))
    result = client.generate(
        SemanticMatcher().request(tender(), CampaignCriteria(name="C"), config, 1)
    )
    assert result.model_version == config.model and result.input_tokens == 42
    assert result.cost is None
    assert client.parameters("another-model") == {}


@pytest.mark.parametrize(
    "status,body",
    [
        (503, {}),
        (200, {"error": {"message": "secret"}}),
        (200, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}),
    ],
)
def test_provider_errors_do_not_become_predictions(status: int, body: dict[str, object]) -> None:
    client = OpenRouterModelClient(
        SecretStr("fake"), httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    )
    result = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(
        tender(), CampaignCriteria(name="C", description="Networks")
    )
    assert result.result == "unsure" and "secret" not in result.model_dump_json()


def test_dotenv_is_data_and_preserves_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXISTING", "old")
    monkeypatch.delenv("TEST_DATA_ONLY", raising=False)
    path = tmp_path / "credentials.env"
    path.write_text("EXISTING=new\nTEST_DATA_ONLY='literal $(false)'\n")
    load_env(path)
    import os

    assert os.environ["EXISTING"] == "old"
    assert os.environ["TEST_DATA_ONLY"] == "literal $(false)"
    monkeypatch.delenv("TEST_DATA_ONLY")


def test_metrics_count_errors_abstention_and_recall_separately() -> None:
    from tender_system.core.domain import MatchResult

    def result(value: str) -> MatchResult:
        return MatchResult.model_validate(
            {"result": value, "score": 0.5, "reasons": [], "uncertainties": []}
        )

    report = metrics(
        [
            ("a", "match", result("unsure")),
            ("b", "no_match", result("match")),
            ("c", "match", result("no_match")),
            ("d", "unsure", result("match")),
        ]
    )
    assert report["false_positives"] == report["false_negatives"] == 1
    assert report["useful_tenders_not_matched"] == 2
    assert report["definitive_on_uncertain_labels"] == 1


def test_semantic_request_uses_document_text_and_keyword_priority() -> None:
    client = ScriptedModelClient((response(),))
    item = tender(document_text="photocopier with obsolete components")
    criteria = CampaignCriteria(
        name="Office", include_keywords=("photocopier",), exclude_keywords=("obsolete",)
    )
    result = IntelligentMatcher(settings(), FakeClock(NOW), lambda _: client).match(item, criteria)
    assert result.result == "match"
    request = client.requests[0]
    assert cast(dict[str, JsonValue], request.payload["tender"])["document_text"] == (
        "photocopier with obsolete components"
    )
    assert not cast(dict[str, JsonValue], request.payload["campaign"])["exclude_keywords"]


def test_evidence_from_omitted_document_text_is_rejected_even_if_present_in_source() -> None:
    from dataclasses import replace

    item = tender(document_text="Supplied clause. Original omitted requirement.")
    matcher = SemanticMatcher()
    request = matcher.request(item, CampaignCriteria(name="Network"), ModelSettings(), 10)
    fields = request.payload["tender"]
    assert isinstance(fields, dict)
    request = replace(
        request,
        payload={
            **request.payload,
            "tender": {
                **fields,
                "document_text": "Supplied clause.",
            },
        },
    )
    proposed = response(
        evidence=[
            {
                "field": "document_text",
                "quote": "Original omitted requirement.",
            }
        ]
    )
    with pytest.raises(ModelFailure, match="invalid_evidence"):
        matcher.validate(proposed, item, request)


@pytest.mark.parametrize("version", ["1", "2", "3"])
@pytest.mark.parametrize("uncertain", ["required_field", "conflicting_criteria"])
def test_uncertainty_cannot_erase_independent_exclusion(version: str, uncertain: str) -> None:
    client = ScriptedModelClient((response(),))
    config = CampaignSettings.model_validate(
        {
            "matcher_adapter": "intelligent",
            "matcher_version": version,
            "uncertainty": {"required_fields": ["buyer"] if uncertain == "required_field" else []},
        }
    )
    criteria = CampaignCriteria(
        name="Exclude",
        description="Equipment",
        exclude_keywords=("medical ventilators",),
        buyers=("Buyer",) if uncertain == "conflicting_criteria" else (),
        exclude_buyers=("Buyer",) if uncertain == "conflicting_criteria" else (),
    )
    outcome = IntelligentMatcher(config, FakeClock(NOW), lambda _: client).match(
        tender(title="Supply medical ventilators", documents_incomplete=True), criteria
    )
    assert outcome.result == "no_match"
    assert any(reason.code == "excluded_keyword" for reason in outcome.reasons)
    assert not client.requests


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_inclusion_priority_does_not_clear_other_mandatory_constraints(version: str) -> None:
    client = ScriptedModelClient((response(),))
    outcome = IntelligentMatcher(
        CampaignSettings(matcher_adapter="intelligent", matcher_version=version),
        FakeClock(NOW),
        lambda _: client,
    ).match(
        tender(title="Ethernet switches and medical ventilators", buyer="Blocked"),
        CampaignCriteria(
            name="Network",
            include_keywords=("Ethernet",),
            exclude_keywords=("medical ventilators",),
            exclude_buyers=("Blocked",),
        ),
    )
    assert outcome.result == "no_match"
    assert any(reason.code == "excluded_buyer" for reason in outcome.reasons)
    assert not any(reason.code == "excluded_keyword" for reason in outcome.reasons)
    assert not client.requests


def test_reference_rules_cannot_reject_missing_keyword_in_unprocessed_documents() -> None:
    from tender_system.matching.matching import RulesMatcher

    result = RulesMatcher(CampaignSettings(), FakeClock(NOW)).match(
        tender(documents_incomplete=True),
        CampaignCriteria(name="Network", matching_mode="strict", include_keywords=("router",)),
    )
    assert result.result == "unsure"


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_unreliable_other_field_cannot_erase_verified_exclusion(version: str) -> None:
    client = ScriptedModelClient(())
    item = tender(
        title="Supply medical ventilators",
        buyer="Uncertain",
        quality_issues=[
            {"field": "buyer", "code": "invalid", "detail": "Unreliable buyer", "severity": "error"}
        ],
    )
    criteria = CampaignCriteria(
        name="Medical exclusion", buyers=("Wanted",), exclude_keywords=("medical ventilators",)
    )
    result = IntelligentMatcher(
        CampaignSettings(matcher_adapter="intelligent", matcher_version=version),
        FakeClock(NOW),
        lambda _: client,
    ).match(item, criteria)
    assert result.result == "no_match"
    assert any(reason.code == "excluded_keyword" for reason in result.reasons)
    assert not client.requests
