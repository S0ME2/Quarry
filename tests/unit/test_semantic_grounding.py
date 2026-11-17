"""Offline source-grounding regressions; recorded opinion is not an accuracy oracle."""

import json
from pathlib import Path

import pytest

from tender_system.core.configuration import ModelSettings
from tender_system.core.domain import CampaignCriteria, Tender
from tender_system.core.ports import ModelRequest
from tender_system.matching.semantic_decision import SemanticDecisionMatcher
from tender_system.matching.semantic_evidence import verify_optional_evidence

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/matcher/tender_8915917.json").read_text()
)


def case() -> tuple[Tender, ModelRequest]:
    tender = Tender.model_validate(FIXTURE["tender"])
    criteria = CampaignCriteria.model_validate(FIXTURE["criteria"])
    return tender, SemanticDecisionMatcher().request(tender, criteria, ModelSettings(), 30)


@pytest.mark.parametrize(
    "field,quote",
    [
        ("title", "мармар, оникс"),
        ("tender.title", "мармар, оникс"),
        ("product_items.0.name", "Работы по строительству нежилых зданий"),
        ("tender.product_candidates.0.name", "Работы по строительству нежилых зданий"),
        (
            "product_items.0.attributes.category.name.uz-UZ@latin",
            "turar joy binolarini qurish ishlari",
        ),
    ],
)
def test_real_supplied_citations(field: str, quote: str) -> None:
    tender, request = case()
    status, evidence = verify_optional_evidence(
        [{"field": field, "quote": quote}], True, tender, request
    )
    assert status.status == "verified", status
    assert len(evidence) == 1
    assert not evidence[0].field.startswith("tender.")


@pytest.mark.parametrize(
    "field,quote",
    [
        ("title", "tabiyat park"),
        ("title", "construction of a park"),
        ("title", "мармар оникс"),
        ("campaign.description", "construction"),
        ("product_items.0.attributes.category", "turar joy"),
        ("product_items.99.name", "Работы по строительству нежилых зданий"),
    ],
)
def test_unsupported_evidence_rejected(field: str, quote: str) -> None:
    tender, request = case()
    status, evidence = verify_optional_evidence(
        [{"field": field, "quote": quote}], True, tender, request
    )
    assert status.status == "invalid"
    assert not evidence


def test_verification_checks_supplied_fields_only() -> None:
    tender, request = case()
    fields = request.payload["tender"]
    assert isinstance(fields, dict)
    fields.pop("buyer", None)
    status, evidence = verify_optional_evidence(
        [{"field": "buyer", "quote": tender.buyer}], True, tender, request
    )
    assert status.status == "invalid"
    assert not evidence


def test_compact_context_preserves_conflicting_scope_and_other_attributes() -> None:
    tender, request = case()
    fields = request.payload["tender"]
    assert isinstance(fields, dict)
    candidates = fields["product_candidates"]
    assert isinstance(candidates, list)
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    attributes = candidate["attributes"]
    assert isinstance(attributes, dict)
    category = attributes["category"]
    assert isinstance(category, dict)
    assert candidate["name"] == "Работы по строительству нежилых зданий"
    assert "жилых зданий" in json.dumps(candidate, ensure_ascii=False)
    assert "Noturar joy" in json.dumps(candidate, ensure_ascii=False)
    assert "uid" not in category
    assert "category_id" not in attributes
    assert "name" not in attributes
    selection = request.payload["product_selection"]
    assert isinstance(selection, dict) and selection["complete"] is True


@pytest.mark.parametrize(
    "title,quote,decision",
    [
        ("Noturar joy binolarini qurish ishlari", "Noturar joy binolarini qurish", "match"),
        ("Нотурар жой биноларини қуриш ишлари", "Нотурар жой биноларини қуриш", "match"),
        ("Поставка серверов без строительных работ", "без строительных работ", "no_match"),
        ("Construction services excluded; supply routers only", "supply routers only", "no_match"),
        ("Qurilish yoki ta’mirlash — hajmi noma’lum", "hajmi noma’lum", "unsure"),
    ],
)
def test_multilingual_reviewed_response_replays(title: str, quote: str, decision: str) -> None:
    from tender_system.core.configuration import CampaignSettings
    from tender_system.core.ports import ModelResponse
    from tender_system.matching.intelligent_matching import IntelligentMatcher
    from tender_system.testing.fakes import FakeClock, ScriptedModelClient

    tender, _ = case()
    tender = tender.model_copy(update={"title": title, "description": None, "product_items": None})
    response = ModelResponse(
        output={
            "decision": decision,
            "confidence": 0.95,
            "explanation": quote,
            "evidence": [{"field": "title", "quote": quote}],
        },
        model_version="offline-reviewed-response",
    )
    client = ScriptedModelClient((response,))
    matcher = IntelligentMatcher(
        CampaignSettings(matcher_version="3"), FakeClock(tender.first_seen_at), lambda _: client
    )
    result = matcher.match(tender, CampaignCriteria.model_validate(FIXTURE["criteria"]))
    assert result.result == decision
    assert result.evidence_verification is not None
    assert result.evidence_verification.status == "verified"
    assert {r.code for r in result.reasons} == {"model_opinion"}


def test_recorded_hallucination_is_not_verified_by_a_legitimate_quote() -> None:
    from tender_system.core.configuration import CampaignSettings
    from tender_system.core.ports import ModelResponse
    from tender_system.matching.intelligent_matching import IntelligentMatcher
    from tender_system.testing.fakes import FakeClock, ScriptedModelClient

    tender, _ = case()
    opinion = FIXTURE["recorded_result"]["model_opinion"]
    # Original raw citations were not retained. This citation is deliberately
    # constructed to show that source presence cannot prove free-text reasoning.
    client = ScriptedModelClient(
        (
            ModelResponse(
                output={**opinion, "evidence": [{"field": "title", "quote": "мармар, оникс"}]},
                model_version="recorded-opinion",
            ),
        )
    )
    result = IntelligentMatcher(
        CampaignSettings(matcher_version="3"), FakeClock(tender.first_seen_at), lambda _: client
    ).match(tender, CampaignCriteria.model_validate(FIXTURE["criteria"]))
    assert result.model_opinion is not None and "park" in result.model_opinion.explanation
    assert {r.code for r in result.reasons} == {"model_opinion"}
    assert all("park" not in e.quote for e in result.evidence)


def test_candidate_positions_resolve_original_indexes_and_omitted_leaves_fail() -> None:
    tender, request = case()
    fields = request.payload["tender"]
    assert isinstance(fields, dict)
    candidates = fields["product_candidates"]
    assert isinstance(candidates, list) and isinstance(candidates[0], dict)
    candidates[0]["source_index"] = 17
    # Candidate position differs from its real source index.
    assert tender.product_items is not None
    tender = tender.model_copy(update={"product_items": [tender.product_items[0]] * 18})
    status, evidence = verify_optional_evidence(
        [{"field": "tender.product_candidates.0.name", "quote": "Работы по строительству"}],
        True,
        tender,
        request,
    )
    assert status.status == "verified" and evidence[0].field == "product_items.17.name"
    status, evidence = verify_optional_evidence(
        [{"field": "product_items.0.name", "quote": "Работы по строительству"}],
        True,
        tender,
        request,
    )
    assert status.status == "invalid" and not evidence


def test_altered_request_cannot_invent_source_facts() -> None:
    tender, request = case()
    fields = request.payload["tender"]
    assert isinstance(fields, dict)
    fields["title"] = "construction of a park"
    status, evidence = verify_optional_evidence(
        [{"field": "title", "quote": "construction of a park"}], True, tender, request
    )
    assert status.status == "invalid" and not evidence


def test_unknown_specifications_are_not_removed_when_text_matches_name() -> None:
    tender, _ = case()
    tender = tender.model_copy(
        update={
            "product_items": [
                {
                    "name": "Steel",
                    "material": "Steel",
                    "destination": "Steel",
                    "specification": {"required": True, "grade": "A", "certificate": "A"},
                }
            ]
        }
    )
    request = SemanticDecisionMatcher().request(
        tender, CampaignCriteria(name="Steel"), ModelSettings(), 30
    )
    fields = request.payload["tender"]
    assert isinstance(fields, dict)
    candidates = fields["product_candidates"]
    assert isinstance(candidates, list) and isinstance(candidates[0], dict)
    attrs = candidates[0]["attributes"]
    assert isinstance(attrs, dict)
    assert attrs["material"] == attrs["destination"] == "Steel"
    assert attrs["specification"] == {"required": True, "grade": "A", "certificate": "A"}
