from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue, ValidationError

from tender_system.collection.health import ConservativeRunHealth, source_freshness
from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.core.configuration import Configuration, SourceSettings
from tender_system.core.domain import (
    CollectionRun,
    CompletionEvidence,
    HealthDecision,
    RunDiagnostics,
    SourceRecord,
    SourceSnapshot,
)
from tender_system.testing.calibration import fixture_source_settings

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE = SourceRecord(id="fixture", name="Finite source fixture")


def observation(identifier: str = "1") -> dict[str, JsonValue]:
    return {"external_tender_id": identifier, "title": "Equipment"}


def settings(**changes: JsonValue) -> SourceSettings:
    values = fixture_source_settings().model_dump()
    values["calibration"].update(changes)
    return SourceSettings.model_validate(values)


def run(
    rows: tuple[dict[str, JsonValue], ...] = (observation(),),
    *,
    policy: SourceSettings | None = None,
    mode: str = "full",
    complete: bool = True,
    proof: bool | None = True,
    as_of: datetime | None = None,
) -> CollectionRun:
    policy = policy or settings()
    normalized = tuple(
        ConfigurableTenderNormalizer().normalize(row, SOURCE, policy).candidate for row in rows
    )
    return CollectionRun.model_validate(
        dict(
            id=uuid4(),
            source_id=SOURCE.id,
            recipe_version="1",
            started_at=NOW,
            finished_at=NOW,
            candidates=tuple(c for c in normalized if c is not None),
            raw_observations=rows,
            complete=complete,
            coverage=mode,
            expected_count=len(rows),
            evidence_reference="fixture:evidence",
            data_as_of=as_of,
            diagnostics=RunDiagnostics(
                raw_count=len(rows),
                completion=None
                if proof is None
                else CompletionEvidence(
                    kind="total_reconciled",
                    observed=proof,
                    value=len(rows),
                    reference="fixture:total",
                ),
            ),
        )
    )


def decision(
    collection: CollectionRun,
    policy: SourceSettings | None = None,
    snapshot: SourceSnapshot | None = None,
) -> HealthDecision:
    return ConservativeRunHealth().evaluate(
        collection, policy or settings(), snapshot or SourceSnapshot(), (), now=NOW
    )


def test_cold_start_uses_positive_structural_evidence_without_historical_minimum() -> None:
    result = decision(run())
    assert (
        result.overall_status == "healthy"
        and result.publication_allowed
        and result.absence_evidence_allowed
    )
    assert result.calibration == "provisional"
    assert "provisional_cold_start" in {reason.code for reason in result.reasons}


def test_nullable_title_with_explicit_object_mapping_and_content_requirement() -> None:
    policy = SourceSettings.model_validate(
        {
            **settings().model_dump(),
            "health": {
                "required_fields": ["external_tender_id"],
                "required_any_of": [["title", "description"]],
            },
            "normalization": {"field_mapping": {"description": "object_text"}},
        }
    )
    row: dict[str, JsonValue] = {
        "external_tender_id": "1",
        "title": None,
        "object_text": "Procurement object: equipment",
    }
    collection = run((row,), policy=policy)
    assert decision(collection, policy).publication_allowed
    candidate = collection.candidates[0]
    assert candidate.title is None and candidate.description == row["object_text"]
    assert (
        candidate.raw_data["title"] is None
        and candidate.raw_data["object_text"] == row["object_text"]
    )


@pytest.mark.parametrize("title,description", [(None, None), ("", " "), (None, "")])
def test_optional_title_does_not_allow_contentless_records(
    title: JsonValue, description: JsonValue
) -> None:
    policy = SourceSettings.model_validate(
        {
            **settings().model_dump(),
            "health": {
                "required_fields": ["external_tender_id"],
                "required_any_of": [["title", "description"]],
            },
        }
    )
    collection = run(
        ({"external_tender_id": "1", "title": title, "description": description},), policy=policy
    )
    result = decision(collection, policy)
    assert not result.publication_allowed
    assert "missing_required_content" in {reason.code for reason in result.reasons}


def test_declared_required_title_still_fails_with_other_content() -> None:
    collection = run(({"external_tender_id": "1", "title": None, "description": "Equipment"},))
    result = decision(collection)
    assert not result.publication_allowed
    assert "missing_required_fields" in {reason.code for reason in result.reasons}


def test_alternative_content_cannot_replace_required_stable_identity() -> None:
    policy = SourceSettings.model_validate(
        {
            **settings().model_dump(),
            "health": {"required_fields": [], "required_any_of": [["title", "description"]]},
        }
    )
    collection = run(({"title": None, "description": "Equipment"},), policy=policy)
    assert not collection.candidates and not decision(collection, policy).publication_allowed


def test_every_required_alternative_group_must_be_satisfied() -> None:
    policy = SourceSettings.model_validate(
        {
            **settings().model_dump(),
            "health": {
                "required_fields": [],
                "required_any_of": [["title", "description"], ["buyer", "location"]],
            },
        }
    )
    collection = run(({"external_tender_id": "1", "description": "Equipment"},), policy=policy)
    assert not decision(collection, policy).publication_allowed
    collection = run(
        ({"external_tender_id": "1", "description": "Equipment", "buyer": "Buyer"},), policy=policy
    )
    assert decision(collection, policy).publication_allowed


@pytest.mark.parametrize(
    "proof,complete,expected",
    [(None, True, "unknown"), (False, True, "incomplete"), (True, False, "incomplete")],
)
def test_scraper_stopping_does_not_prove_inventory_completion(
    proof: bool | None, complete: bool, expected: str
) -> None:
    result = decision(run(proof=proof, complete=complete))
    assert (
        result.completeness == expected
        and not result.publication_allowed
        and not result.absence_evidence_allowed
    )


def test_unknown_source_is_not_silently_full_inventory_capable() -> None:
    result = decision(run(), SourceSettings())
    assert result.overall_status == "unknown" and not result.absence_evidence_allowed


def test_incremental_completion_never_establishes_absence() -> None:
    result = decision(run(mode="incremental"))
    assert (
        result.publication_allowed
        and result.completeness == "complete"
        and not result.absence_evidence_allowed
    )


def test_reconciled_incremental_window_can_publish_without_site_exhaustion_or_source_time() -> None:
    policy = settings(
        expected_mode="incremental",
        full_inventory_supported=False,
        freshness={"basis": "unknown", "unknown_policy": "observe_only"},
    )
    result = decision(run(policy=policy, mode="incremental", proof=None), policy)

    assert result.publication_allowed
    assert result.completeness == "complete"
    assert result.dimensions["inventory"] == "unknown"
    assert result.freshness.status == "unknown"
    assert result.freshness.timestamp is None
    assert not result.absence_evidence_allowed
    assert "completion_signal_unproven" not in {reason.code for reason in result.reasons}
    assert any(
        reason.code == "missing_freshness_evidence" and reason.severity == "warning"
        for reason in result.reasons
    )


def test_incremental_window_must_reconcile_its_selected_records() -> None:
    policy = settings(expected_mode="incremental", full_inventory_supported=False)
    collection = run(policy=policy, mode="incremental", proof=None).model_copy(
        update={"expected_count": None}
    )

    result = decision(collection, policy)

    assert not result.publication_allowed
    assert "incremental_window_unreconciled" in {reason.code for reason in result.reasons}
    assert not result.absence_evidence_allowed


def test_unknown_full_capability_not_manufactured_by_boolean_true() -> None:
    policy = settings(full_inventory_supported=False)
    result = decision(run(policy=policy), policy)
    assert result.completeness == "unknown" and not result.absence_evidence_allowed


@pytest.mark.parametrize(
    "basis,as_of,status",
    [
        ("inventory_timestamp", NOW - timedelta(days=2), "stale"),
        ("inventory_timestamp", None, "unknown"),
        ("inventory_timestamp", NOW + timedelta(seconds=1), "stale"),
        ("inventory_timestamp", NOW - timedelta(minutes=10), "fresh"),
        ("unknown", NOW, "unknown"),
    ],
)
def test_source_timestamp_semantics_are_explicit(
    basis: str, as_of: datetime | None, status: str
) -> None:
    policy = settings(
        freshness={
            "basis": basis,
            "timestamp_meaning": "Authoritative inventory snapshot cutoff",
            "max_age_seconds": 3600,
        }
    )
    result = decision(run(policy=policy, as_of=as_of), policy)
    assert result.freshness.status == status
    assert result.absence_evidence_allowed == (status == "fresh")
    assert result.freshness.timestamp == (as_of if basis == "inventory_timestamp" else None)


def test_aging_data_may_publish_observed_records_but_never_absence() -> None:
    policy = settings(
        freshness={
            "basis": "inventory_timestamp",
            "timestamp_meaning": "Inventory snapshot",
            "max_age_seconds": 3600,
            "aging_after_seconds": 300,
        }
    )
    result = decision(run(as_of=NOW - timedelta(minutes=10)), policy)
    assert (
        result.overall_status == "degraded"
        and result.publication_allowed
        and not result.absence_evidence_allowed
    )
    assert result.freshness.status == "aging"


def test_unknown_freshness_can_only_publish_when_observe_only_is_explicit() -> None:
    policy = settings(freshness={"basis": "unknown", "unknown_policy": "observe_only"})
    result = decision(run(as_of=NOW), policy)
    assert (
        result.overall_status == "degraded"
        and result.publication_allowed
        and not result.absence_evidence_allowed
    )
    assert result.freshness.timestamp is None  # generated_at does not invent content freshness


def test_replayed_source_timestamp_cannot_advance_absence() -> None:
    policy = settings(
        freshness={"basis": "inventory_timestamp", "timestamp_meaning": "Inventory cutoff"}
    )
    result = decision(run(as_of=NOW), policy, SourceSnapshot(last_source_timestamp=NOW))
    assert not result.publication_allowed and not result.absence_evidence_allowed
    assert "source_timestamp_not_advanced" in {r.code for r in result.reasons}


def test_valid_empty_requires_both_terminal_and_source_empty_evidence() -> None:
    policy = SourceSettings.model_validate(
        {**settings().model_dump(), "health": {"allow_empty": True}}
    )
    result = decision(run(rows=(), policy=policy), policy)
    assert result.empty_classification == "valid_empty" and result.absence_evidence_allowed
    unknown = decision(
        run(rows=(), policy=policy).model_copy(update={"expected_count": None}), policy
    )
    assert unknown.empty_classification == "unknown_empty" and not unknown.absence_evidence_allowed
    incomplete = decision(run(rows=(), policy=policy, proof=None), policy)
    assert not incomplete.absence_evidence_allowed


def test_parser_empty_is_suspicious_even_when_zero_is_allowed() -> None:
    policy = SourceSettings.model_validate(
        {**settings().model_dump(), "health": {"allow_empty": True}}
    )
    result = decision(run(rows=({**observation(), "deadline": "broken"},), policy=policy), policy)
    assert result.empty_classification == "suspicious_empty" and not result.publication_allowed
    assert result.malformed_count == 1


@pytest.mark.parametrize("ratio,allowed", [(0.2, False), (0.5, True)])
def test_malformed_tolerance_does_not_authorize_absence(ratio: float, allowed: bool) -> None:
    policy = settings(maximum_malformed_ratio=ratio)
    result = decision(
        run(rows=(observation("1"), observation("2"), {**observation("3"), "value": "bad"})), policy
    )
    assert result.publication_allowed == allowed and not result.absence_evidence_allowed
    reason = next(r for r in result.reasons if r.code == "normalization_failed")
    assert reason.observed == 1 / 3 and reason.threshold == ratio


def test_duplicate_tolerance_and_conflicting_duplicates_fail_safely() -> None:
    policy = settings(maximum_duplicate_ratio=0.5)
    good = decision(run(rows=(observation("1"), observation("1"), observation("2"))), policy)
    assert (
        good.publication_allowed
        and good.overall_status == "degraded"
        and not good.absence_evidence_allowed
    )
    bad = decision(
        run(
            rows=(observation("1"), {**observation("1"), "title": "Conflicting"}, observation("2"))
        ),
        policy,
    )
    assert not bad.publication_allowed and not bad.absence_evidence_allowed


def test_incremental_liveness_does_not_refresh_old_verified_full_inventory() -> None:
    policy = settings()
    snapshot = SourceSnapshot(
        last_full_verified_at=NOW - timedelta(days=2),
        last_full_freshness_at=NOW - timedelta(days=2),
        last_full_policy=policy.calibration.version,
        last_full_freshness_basis="observed",
        last_incremental_at=NOW,
    )
    current = source_freshness(snapshot, policy, NOW)
    assert (
        current["full_inventory"].status == "stale"
        and current["incremental_liveness"].status == "fresh"
    )
    assert (
        source_freshness(snapshot, settings(version="new-semantics"), NOW)["full_inventory"].status
        == "unknown"
    )


@pytest.mark.parametrize(
    "calibration",
    [
        {"status": "calibrated"},
        {"full_inventory_supported": True},
        {"freshness": {"basis": "inventory_timestamp"}},
        {"freshness": {"basis": "observed"}},
        {"empty_policy": "signature"},
        {"freshness": {"max_age_seconds": 1, "aging_after_seconds": 2}},
    ],
)
def test_invalid_or_unsupported_calibration_is_rejected(calibration: dict[str, JsonValue]) -> None:
    with pytest.raises(ValidationError):
        SourceSettings.model_validate({"calibration": calibration})


def test_calibration_overrides_merge_and_do_not_leak_between_sources() -> None:
    config = Configuration.model_validate(
        {
            "global": {"source": {"calibration": {"freshness": {"max_age_seconds": 3600}}}},
            "sources": {
                "a": {
                    "calibration": {
                        "version": "a",
                        "full_inventory_supported": True,
                        "completion_signals": ["next_cursor_absent"],
                        "freshness": {
                            "basis": "observed",
                            "timestamp_meaning": "API returns current uncached state",
                        },
                    }
                }
            },
        }
    )
    assert config.for_source("a").calibration.freshness.max_age_seconds == 3600
    assert not config.for_source("b").calibration.full_inventory_supported
    assert config.for_source("b").calibration.freshness.basis == "unknown"


@given(st.booleans(), st.booleans(), st.sampled_from(["full", "incremental"]))
def test_absence_implication_property(complete: bool, proof: bool, mode: str) -> None:
    result = decision(run(complete=complete, proof=proof, mode=mode))
    if result.absence_evidence_allowed:
        assert complete and proof and mode == "full" and result.publication_allowed
        assert result.completeness == "complete" and result.overall_status == "healthy"


@pytest.mark.parametrize(
    "kind,value",
    [
        ("next_cursor_absent", None),
        ("end_marker", True),
        ("final_page", True),
        ("empty_terminal_page", True),
    ],
)
def test_literal_terminal_signals_cannot_fabricate_completion(kind: str, value: JsonValue) -> None:
    from tender_system.core.domain import CompletionSpec

    with pytest.raises(ValidationError, match="explicit.*ref"):
        CompletionSpec.model_validate({"kind": kind, "value": value})


def test_delayed_publication_rechecks_current_age() -> None:
    result = ConservativeRunHealth().evaluate(
        run(), settings(), SourceSnapshot(), (), now=NOW + timedelta(days=2)
    )
    assert result.freshness.status == "stale" and not result.publication_allowed
    assert not result.absence_evidence_allowed


def test_cold_start_freshness_does_not_invent_an_observation_time() -> None:
    result = source_freshness(SourceSnapshot(), settings(), NOW)
    assert all(value.status == "unknown" and value.observed_at is None for value in result.values())


def test_required_page_and_total_invariants_fail_safely() -> None:
    policy = settings(minimum_pages=1, require_reported_total=True)
    result = decision(run().model_copy(update={"expected_count": None}), policy)
    codes = {reason.code for reason in result.reasons}
    assert {"insufficient_pages", "reported_total_missing"} <= codes
    assert not result.publication_allowed and not result.absence_evidence_allowed


def test_legacy_unverified_count_cannot_become_a_hidden_cold_start_requirement() -> None:
    result = decision(run(), snapshot=SourceSnapshot(last_full_count=100, accepted_full_runs=0))
    assert result.publication_allowed and result.absence_evidence_allowed
    assert "suspicious_count_drop" not in {r.code for r in result.reasons}


@pytest.mark.parametrize(
    "mode,complete,proof,expected",
    [
        ("full", True, True, "verified_full_inventory"),
        ("full", False, True, "incomplete"),
        ("full", True, None, "uncertain"),
        ("incremental", True, True, "uncertain"),
        ("sampled", False, True, "incomplete"),
    ],
)
def test_coverage_summary_does_not_confuse_window_and_source_completeness(
    mode: str,
    complete: bool,
    proof: bool | None,
    expected: str,
) -> None:
    from tender_system.operator.operations import coverage_status

    health = decision(run(mode=mode, complete=complete, proof=proof))
    assert coverage_status(health) == expected
    assert coverage_status(None) == "uncertain"
