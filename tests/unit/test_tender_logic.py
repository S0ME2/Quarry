from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tender_system.core.configuration import NormalizationSettings, SourceSettings
from tender_system.core.domain import Tender, TenderCandidate, TenderPresence, TenderVersion
from tender_system.core.tenders import (
    ConfigurableTenderValidity,
    HistoryCorruption,
    IdentityConflict,
    SemanticTenderComparator,
    reconstruct_history,
    state_of,
)

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def candidate(**changes: object) -> TenderCandidate:
    return TenderCandidate.model_validate(
        {
            "source_id": "s",
            "external_tender_id": "1",
            "title": "Equipment",
            "buyer": "Buyer",
            "description": None,
            **changes,
        }
    )


def stored(value: TenderCandidate | None = None, **changes: object) -> Tender:
    return Tender.model_validate(
        {
            **(value or candidate()).model_dump(),
            "internal_id": uuid4(),
            "current_version_id": uuid4(),
            "first_seen_at": NOW - timedelta(days=10),
            "last_seen_at": NOW - timedelta(days=2),
            **changes,
        }
    )


def test_semantic_comparison_ignores_raw_metadata_case_whitespace_and_equal_money() -> None:
    before = stored(candidate(title="A  Tender", value="100.00", raw_data={"revision": 1}))
    after = candidate(title="a\n tender", value="100", raw_data={"revision": 2})
    diff = SemanticTenderComparator().compare(before, after, ("title", "value"))
    assert diff.classification == "same" and diff.changed_fields == {}
    assert diff.requires_rematch is False


def test_case_sensitive_field_is_configurable() -> None:
    comparator = SemanticTenderComparator(NormalizationSettings(case_sensitive_fields=("title",)))
    assert comparator.compare(stored(), candidate(title="EQUIPMENT"), ("title",)).requires_rematch


def test_semantic_money_comparison_preserves_precision_beyond_decimal_context() -> None:
    first = "123456789012345678901234567890.01"
    second = "123456789012345678901234567890.02"
    comparator = SemanticTenderComparator()
    assert (
        comparator.compare(
            stored(candidate(value=first)), candidate(value=first + "0"), ("value",)
        ).classification
        == "same"
    )
    diff = comparator.compare(stored(candidate(value=first)), candidate(value=second), ("value",))
    assert diff.changed_fields == {"value": second} and diff.requires_rematch


def test_change_to_nonrematch_field_updates_history_decision_only() -> None:
    diff = SemanticTenderComparator().compare(stored(), candidate(buyer="New Buyer"), ("title",))
    assert diff.changed_fields == {"buyer": "New Buyer"}
    assert diff.requires_rematch is False
    assert (
        SemanticTenderComparator()
        .compare(stored(), candidate(buyer="New Buyer"), ("buyer",))
        .requires_rematch
    )


def test_comparison_refuses_identity_changes_and_unknown_rematch_keys() -> None:
    with pytest.raises(IdentityConflict):
        SemanticTenderComparator().compare(stored(), candidate(source_id="other"), ())
    with pytest.raises(ValueError):
        SemanticTenderComparator().compare(stored(), candidate(), ("raw_data",))


def test_dst_fold_difference_is_a_real_change() -> None:
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    diff = SemanticTenderComparator().compare(
        stored(candidate(deadline=first)), candidate(deadline=second), ("deadline",)
    )
    assert diff.requires_rematch and set(diff.changed_fields) == {"deadline"}


@given(st.lists(st.one_of(st.none(), st.text(max_size=50)), min_size=1, max_size=15))
def test_diff_and_full_history_reconstruct_each_state(values: list[str | None]) -> None:
    comparator = SemanticTenderComparator()
    current: Tender | None = None
    versions: list[TenderVersion] = []
    for value in values:
        next_candidate = candidate(description=value)
        diff = comparator.compare(current, next_candidate, ("description",))
        if diff.classification == "same":
            continue
        previous_id = current.current_version_id if current else None
        version_id = uuid4()
        next_state = (state_of(current) if current else {}) | diff.changed_fields
        current = stored(
            TenderCandidate.model_validate(next_state),
            internal_id=current.internal_id if current else uuid4(),
            current_version_id=version_id,
        )
        versions.append(
            TenderVersion(
                id=version_id,
                tender_id=current.internal_id,
                previous_version_id=previous_id,
                changed_fields=diff.changed_fields,
                observed_at=NOW,
                evidence_reference="fixture:property",
            )
        )
        assert reconstruct_history(tuple(reversed(versions))) == state_of(current)
        assert reconstruct_history(tuple(versions), version_id) == next_state
    assert current is not None


def test_reconstruction_rejects_incomplete_forked_or_disconnected_history() -> None:
    tender_id = uuid4()
    initial = TenderVersion(
        id=uuid4(),
        tender_id=tender_id,
        previous_version_id=None,
        changed_fields=state_of(candidate()),
        observed_at=NOW,
        evidence_reference="fixture",
    )
    child = TenderVersion(
        id=uuid4(),
        tender_id=tender_id,
        previous_version_id=initial.id,
        changed_fields={"description": "new"},
        observed_at=NOW,
        evidence_reference="fixture",
    )
    fork = child.model_copy(update={"id": uuid4()})
    invalid_cases = [
        (),
        (initial.model_copy(update={"changed_fields": {"title": "x"}}),),
        (initial, child, fork),
        (initial, child.model_copy(update={"previous_version_id": uuid4()})),
        (initial, child.model_copy(update={"changed_fields": {"source_id": "other"}})),
        (initial, child.model_copy(update={"changed_fields": {"value": "bad"}})),
        (initial, child, child),
    ]
    for history in invalid_cases:
        with pytest.raises(HistoryCorruption):
            reconstruct_history(history)


@pytest.mark.parametrize("status", ["closed", "cancelled", " CLOSED "])
def test_closed_and_cancelled_are_invalid_without_deleting_history(status: str) -> None:
    decision = ConfigurableTenderValidity().evaluate(
        stored(candidate(status=status)), SourceSettings(), NOW
    )
    assert decision.matchable is False
    assert decision.reasons[0].code == "excluded_status"


def test_expiry_includes_exact_deadline_and_is_configurable() -> None:
    tender = stored(candidate(deadline=NOW))
    assert not ConfigurableTenderValidity().evaluate(tender, SourceSettings(), NOW).matchable
    configured = SourceSettings.model_validate(
        {"validity": {"exclude_expired": False, "excluded_statuses": []}}
    )
    assert ConfigurableTenderValidity().evaluate(tender, configured, NOW).matchable


def test_missing_threshold_and_age_fallbacks_are_configurable() -> None:
    policy = ConfigurableTenderValidity()
    tender = stored(
        presence=TenderPresence(missing_successful_runs=2, last_accounted_run_sequence=20)
    )
    assert policy.evaluate(tender, SourceSettings(), NOW).matchable
    settings = SourceSettings.model_validate({"validity": {"missing_successful_runs": 2}})
    assert not policy.evaluate(tender, settings, NOW).matchable
    age_settings = SourceSettings.model_validate(
        {"validity": {"max_age_days": 5, "max_unseen_days": 1}}
    )
    reasons = policy.evaluate(stored(), age_settings, NOW).reasons
    assert {reason.code for reason in reasons} == {"too_old", "unseen"}
    assert policy.evaluate(
        stored(candidate(deadline=NOW + timedelta(days=1))), age_settings, NOW
    ).matchable
    strict_age = SourceSettings.model_validate(
        {"validity": {"max_age_days": 5, "age_fallback_only": False}}
    )
    assert not policy.evaluate(
        stored(candidate(deadline=NOW + timedelta(days=1))), strict_age, NOW
    ).matchable


def test_validity_refuses_naive_current_time() -> None:
    with pytest.raises(ValueError):
        ConfigurableTenderValidity().evaluate(stored(), SourceSettings(), datetime(2026, 10, 4))
