from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tender_system.core.domain import (
    BrowserStep,
    CampaignCriteria,
    ConditionStep,
    CriteriaVersion,
    DeadlineRules,
    HttpStep,
    MatchResult,
    Reason,
    RecipeStep,
    SourceRecipe,
    TenderCandidate,
    TenderIdentity,
    TenderVersion,
)


def test_external_id_is_primary_identity() -> None:
    identity = TenderIdentity(source_id="source", external_tender_id="123")
    assert identity.source_has_stable_external_id
    assert identity.fallback_kind is None


@pytest.mark.parametrize("kind", ["url", "fingerprint"])
def test_fallback_requires_explicit_source_without_stable_id(kind: str) -> None:
    identity = TenderIdentity.model_validate(
        {
            "source_id": "source",
            "source_has_stable_external_id": False,
            "fallback_kind": kind,
            "fallback_key": "stable-key",
        }
    )
    assert identity.external_tender_id is None
    assert identity.fallback_key == "stable-key"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"fallback_kind": "url", "fallback_key": "example"},
        {"source_has_stable_external_id": False},
        {"external_tender_id": "123", "fallback_key": "example"},
        {"external_tender_id": ""},
        {"external_tender_id": " "},
    ],
)
def test_identity_rejects_missing_ids_and_inconsistent_fallbacks(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TenderIdentity.model_validate({"source_id": "source", **fields})


def test_optional_scalar_location_and_exact_money_roundtrip() -> None:
    base = {
        "source_id": "s",
        "external_tender_id": "1",
        "source_url": "https://fixture.test/1",
        "title": "Tender",
    }
    candidate = TenderCandidate.model_validate(base)
    assert candidate.location is None
    candidate = TenderCandidate.model_validate(
        {**base, "location": "Tashkent", "value": "123456789.123456789"}
    )
    restored = TenderCandidate.model_validate_json(candidate.model_dump_json())
    assert restored.value == Decimal("123456789.123456789")
    assert restored.location == "Tashkent"
    assert restored.source_id == "s"
    assert restored.external_tender_id == "1"
    assert "source_id" in restored.model_dump()


@pytest.mark.parametrize("score", [-1, 1.01, float("nan"), float("inf")])
def test_invalid_match_scores(score: float) -> None:
    with pytest.raises(ValidationError):
        MatchResult(result="match", score=score, reasons=(), uncertainties=())


@pytest.mark.parametrize("outcome", ["match", "no_match", "unsure"])
def test_structured_match_result(outcome: str) -> None:
    result = MatchResult.model_validate(
        {
            "result": outcome,
            "score": 0.5,
            "reasons": [{"code": "keyword", "detail": "Contains configured keyword"}],
            "uncertainties": [],
        }
    )
    assert result.reasons[0].code == "keyword"
    assert MatchResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "fields",
    [
        {"result": "maybe", "score": 0.5, "reasons": [], "uncertainties": []},
        {"result": "match", "score": 0.5},
        {"result": "match", "score": 0.5, "reasons": ["plain string"], "uncertainties": []},
        {"result": "match", "score": 0.5, "reasons": [], "uncertainties": [], "extra": 1},
    ],
)
def test_malformed_model_output_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MatchResult.model_validate(fields)


def test_criteria_contract_includes_all_v1_fields_and_is_frozen() -> None:
    criteria = CampaignCriteria(
        name="IT",
        description="Network equipment",
        include_keywords=("router",),
        exclude_keywords=("used",),
        min_value=Decimal("10"),
        max_value=Decimal("100"),
        currencies=("USD",),
        buyers=("Buyer",),
        locations=("Tashkent",),
        deadline_rules=DeadlineRules(min_days_remaining=2),
        status_rules=("open",),
        matching_mode="strict",
        custom_notes="New equipment only",
    )
    version = CriteriaVersion(
        id=uuid4(), campaign_id=uuid4(), criteria=criteria, created_at=datetime.now(UTC)
    )
    assert CriteriaVersion.model_validate_json(version.model_dump_json()) == version
    with pytest.raises(ValidationError, match="frozen"):
        criteria.name = "changed"  # type: ignore[misc]


def test_invalid_criteria_ranges() -> None:
    with pytest.raises(ValidationError):
        CampaignCriteria(name="Invalid", min_value=Decimal(20), max_value=Decimal(10))
    with pytest.raises(ValidationError):
        DeadlineRules(min_days_remaining=5, max_days_remaining=2)


def test_version_payload_distinguishes_explicit_null_from_absent() -> None:
    version = TenderVersion(
        id=uuid4(),
        tender_id=uuid4(),
        previous_version_id=None,
        changed_fields={"deadline": None, "title": "New title"},
        observed_at=datetime.now(UTC),
        evidence_reference="run:1",
    )
    restored = TenderVersion.model_validate_json(version.model_dump_json())
    assert "deadline" in restored.changed_fields
    assert restored.changed_fields["deadline"] is None
    assert "buyer" not in restored.changed_fields


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CriteriaVersion(
            id=uuid4(),
            campaign_id=uuid4(),
            criteria=CampaignCriteria(name="C"),
            created_at=datetime(2026, 1, 1),
        )


def test_recipe_preserves_nested_steps_and_rejects_future_schema() -> None:
    recipe = SourceRecipe(
        source_id="s",
        version="1",
        steps=(
            RecipeStep(
                type="condition",
                parameters=ConditionStep(predicate=True),
                steps=(
                    RecipeStep(
                        type="http_request", parameters=HttpStep(url="https://fixture.test")
                    ),
                ),
                else_steps=(
                    RecipeStep(type="browser_action", parameters=BrowserStep(action="content")),
                ),
            ),
        ),
    )
    assert SourceRecipe.model_validate_json(recipe.model_dump_json()) == recipe
    with pytest.raises(ValidationError):
        SourceRecipe.model_validate(
            {"schema_version": 2, "source_id": "s", "version": "1", "steps": []}
        )


@given(st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False))
def test_valid_score_roundtrip(score: float) -> None:
    result = MatchResult(
        result="unsure",
        score=score,
        reasons=(Reason(code="pending", detail="Needs evidence"),),
        uncertainties=(),
    )
    assert MatchResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    ("decision", "failure", "invalid", "status", "relevance"),
    [
        ("match", None, False, "finalized", "RELEVANT"),
        ("no_match", None, False, "finalized", "NOT_RELEVANT"),
        ("unsure", None, False, "needs_review", None),
        ("unsure", "timeout", False, "failed", None),
        ("no_match", "rate_limited", False, "failed", None),
        ("match", None, True, "needs_review", None),
    ],
)
def test_final_relevance_is_binary_and_separate_from_processing(
    decision: str, failure: str | None, invalid: bool, status: str, relevance: str | None
) -> None:
    diagnostic = {
        "result": decision,
        "score": 1,
        "reasons": [{"code": "evaluation", "detail": "Recorded evaluation explanation"}],
        "uncertainties": [],
        "model_calls": [
            {"stage": "semantic", "provider": "fixture", "model": "fixture", "error_code": failure}
        ]
        if failure
        else [],
        "evidence_verification": {"status": "invalid"} if invalid else None,
    }
    outcome = MatchResult.model_validate(diagnostic)
    data = outcome.model_dump(mode="json")
    assert data["processing_status"] == status
    assert data["relevance"] == relevance
    assert outcome.reasons[0].detail == "Recorded evaluation explanation"
    assert MatchResult.model_validate(data) == outcome
    # Neither client-supplied projections nor stale stored projections grant a decision.
    data.update(processing_status="finalized", relevance="RELEVANT")
    assert MatchResult.model_validate(data).model_dump(mode="json")["relevance"] == relevance
