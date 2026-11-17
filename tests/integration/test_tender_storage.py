from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, current_thread
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import Connection, Engine, event, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from tender_system.collection.normalization import ConfigurableTenderNormalizer
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    SourceRecord,
    TenderCandidate,
    TenderIdentity,
    TenderObservation,
)
from tender_system.core.ports import TenderRepository
from tender_system.core.tenders import (
    ConfigurableTenderValidity,
    IdempotencyConflict,
    IdentityConflict,
    StaleObservation,
    reconstruct_history,
    state_of,
)
from tender_system.persistence.storage import (
    SqlSourceRepository,
    SqlTenderRepository,
    TenderObservationRow,
    TenderRow,
    TenderVersionRow,
)

pytestmark = pytest.mark.database
NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def candidate(**changes: object) -> TenderCandidate:
    return TenderCandidate.model_validate(
        {
            "source_id": "s",
            "external_tender_id": "001",
            "source_url": "https://source.test/1?a=1#part",
            "title": "Equipment",
            "raw_data": {"raw_title": " Equipment "},
            **changes,
        }
    )


def observation(
    value: TenderCandidate | None = None, *, at: datetime = NOW, observation_id: UUID | None = None
) -> TenderObservation:
    return TenderObservation(
        id=observation_id or uuid4(),
        candidate=value or candidate(),
        observed_at=at,
        evidence_reference="fixture:original-page",
        source_run_id=uuid4(),
    )


def register(session: Session, source_id: str = "s", stable: bool = True) -> None:
    SqlSourceRepository(session).add(
        SourceRecord(id=source_id, name="Source", has_stable_external_id=stable)
    )


def test_product_only_update_versions_and_identical_repeat_is_stable(
    migrated_engine: Engine,
) -> None:
    source = SourceRecord(id="s", name="Source")
    settings = SourceSettings()

    def products(name: str) -> TenderCandidate:
        result = ConfigurableTenderNormalizer().normalize(
            {"external_tender_id": "001", "product_items": [{"name": name, "quantity": 2}]},
            source,
            settings,
        )
        assert result.candidate is not None
        return result.candidate

    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        first = repo.observe(observation(products("Router")), settings.effective_rematch_fields)
        changed = repo.observe(
            observation(products("Switch"), at=NOW + timedelta(seconds=1)),
            settings.effective_rematch_fields,
        )
        assert changed.comparison.classification == "changed"
        assert changed.comparison.requires_rematch
        assert changed.tender.current_version_id != first.tender.current_version_id
        assert changed.comparison.changed_fields == {
            "product_items": [{"name": "Switch", "quantity": 2}]
        }
        repeated = repo.observe(
            observation(products("Switch"), at=NOW + timedelta(seconds=2)),
            settings.effective_rematch_fields,
        )
        assert repeated.comparison.classification == "same"
        assert len(repo.history(first.tender.internal_id)) == 2


def test_replay_has_one_tender_version_and_observation(migrated_engine: Engine) -> None:
    seen = observation()
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo: TenderRepository = SqlTenderRepository(session)
        first = repo.observe(seen, ("title",))
        second = repo.observe(seen, ("title",))
        assert first.tender == second.tender
        assert second.replayed and not second.comparison.requires_rematch
        assert len(repo.history(first.tender.internal_id)) == 1
        assert repo.observations(first.tender.internal_id) == (seen,)
        assert len(repo.for_source("s")) == 1
        assert first.tender.presence.missing_successful_runs == 0


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"description": "New"}, {"description": "New"}),
        ({"description": "New", "buyer": "Buyer"}, {"description": "New", "buyer": "Buyer"}),
        ({"value": "123456789.123456789"}, {"value": "123456789.123456789"}),
    ],
)
def test_only_changed_values_are_stored_in_linked_diff(
    migrated_engine: Engine, changes: dict[str, object], expected: dict[str, JsonValue]
) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        first = repo.observe(observation(), ("description", "value"))
        second = repo.observe(
            observation(candidate(**changes), at=NOW + timedelta(seconds=1)),
            ("description", "value"),
        )
        history = repo.history(first.tender.internal_id)
        assert len(history) == 2
        assert history[0].previous_version_id is None
        assert history[1].previous_version_id == history[0].id
        assert history[1].changed_fields == expected
        assert second.tender.current_version_id == history[1].id
        assert history[1].observation_id == second.tender.latest_observation_id
        assert reconstruct_history(history) == state_of(second.tender)


def test_null_transitions_and_every_historical_state(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        initial = repo.observe(observation(candidate(description=None)), ("description",))
        middle = repo.observe(
            observation(candidate(description="Details"), at=NOW + timedelta(seconds=1)),
            ("description",),
        )
        final = repo.observe(
            observation(candidate(description=None), at=NOW + timedelta(seconds=2)),
            ("description",),
        )
        history = repo.history(initial.tender.internal_id)
        assert history[1].changed_fields == {"description": "Details"}
        assert history[2].changed_fields == {"description": None}
        for result in (initial, middle, final):
            assert reconstruct_history(history, result.tender.current_version_id) == state_of(
                result.tender
            )
        assert len(repo.observations(initial.tender.internal_id)) == 3


def test_fresh_unchanged_observation_updates_evidence_not_history(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        first_observation = observation(candidate(title="A Tender", value="100.00"))
        first = repo.observe(first_observation, ("title", "value"))
        second_observation = observation(
            candidate(title="a\n tender", value="100", raw_data={"title": "a\n tender"}),
            at=NOW + timedelta(days=1),
        )
        second = repo.observe(second_observation, ("title", "value"))
        assert second.comparison.classification == "same"
        assert second.tender.title == "A Tender"
        assert second.tender.last_seen_at == second_observation.observed_at
        assert second.tender.raw_data == second_observation.candidate.raw_data
        assert second.tender.current_version_id == first.tender.current_version_id
        assert len(repo.history(first.tender.internal_id)) == 1
        # Observation reconstruction preserves exactly what normalizer supplied,
        # even when semantic comparison retained old display spelling/decimal scale.
        assert repo.observations(first.tender.internal_id) == (
            first_observation,
            second_observation,
        )


def test_nonrematch_and_configured_rematch_changes(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        first = repo.observe(observation(), ("title",))
        nonrematch = repo.observe(
            observation(candidate(buyer="Buyer"), at=NOW + timedelta(seconds=1)), ("title",)
        )
        rematch = repo.observe(
            observation(candidate(buyer="Buyer", title="Changed"), at=NOW + timedelta(seconds=2)),
            ("title",),
        )
        assert not nonrematch.comparison.requires_rematch
        assert rematch.comparison.requires_rematch
        assert len(repo.history(first.tender.internal_id)) == 3


def test_same_external_id_is_separate_per_source(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        register(session, "other")
        repo = SqlTenderRepository(session)
        first = repo.observe(observation(), ())
        second = repo.observe(observation(candidate(source_id="other")), ())
        assert first.tender.internal_id != second.tender.internal_id
        assert repo.find(TenderIdentity(source_id="s", external_tender_id="001")) == first.tender
        assert (
            repo.find(TenderIdentity(source_id="other", external_tender_id="001")) == second.tender
        )


@pytest.mark.parametrize("fallback", ["url", "fingerprint"])
def test_explicit_fallback_identity_is_persisted_and_idempotent(
    migrated_engine: Engine, fallback: str
) -> None:
    source = SourceRecord(id="s", name="Source", has_stable_external_id=False)
    raw: dict[str, JsonValue] = {
        "source_url": "https://source.test/1?id=A#section",
        "reference": "001",
    }
    settings = SourceSettings.model_validate(
        {"identity": {"url_is_reliable": fallback == "url", "fingerprint_fields": ["reference"]}}
    )
    normalized = ConfigurableTenderNormalizer().normalize(raw, source, settings)
    assert normalized.candidate is not None
    with Session(migrated_engine) as session, session.begin():
        register(session, stable=False)
        repo = SqlTenderRepository(session)
        first = repo.observe(observation(normalized.candidate), ())
        second = repo.observe(observation(normalized.candidate, at=NOW + timedelta(seconds=1)), ())
        assert first.tender.internal_id == second.tender.internal_id
        assert first.tender.fallback_kind == fallback
        assert len(repo.history(first.tender.internal_id)) == 1


def test_old_replay_never_reverts_current_state_and_collision_is_rejected(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        old = observation()
        first = repo.observe(old, ())
        latest = repo.observe(
            observation(candidate(title="Latest"), at=NOW + timedelta(days=1)), ()
        )
        assert repo.observe(old, ()).tender == latest.tender
        with pytest.raises(IdempotencyConflict):
            repo.observe(old.model_copy(update={"evidence_reference": "other-evidence"}), ())
        with pytest.raises(StaleObservation):
            repo.observe(observation(candidate(title="Stale"), at=NOW), ())
        assert len(repo.history(first.tender.internal_id)) == 2
        assert len(repo.observations(first.tender.internal_id)) == 2


def test_unknown_source_or_wrong_identity_policy_is_rejected(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        repo = SqlTenderRepository(session)
        with pytest.raises(IdentityConflict):
            repo.observe(observation(), ())
        register(session, stable=False)
        with pytest.raises(IdentityConflict):
            repo.observe(observation(), ())
        assert repo.for_source("s") == ()


def test_outer_rollback_leaves_no_tender_version_or_provenance(migrated_engine: Engine) -> None:
    with pytest.raises(RuntimeError):
        with Session(migrated_engine) as session, session.begin():
            register(session)
            SqlTenderRepository(session).observe(observation(), ())
            raise RuntimeError("process interrupted before commit")
    with Session(migrated_engine) as session:
        for model in (TenderRow, TenderVersionRow, TenderObservationRow):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_midwrite_failure_rolls_back_version_and_current_state_together(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        first = repo.observe(observation(), ())

        def fail_observation_insert(
            connection: Connection,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            if statement.startswith("INSERT INTO tender_observations"):
                raise RuntimeError("fault after current/version writes")

        event.listen(migrated_engine, "before_cursor_execute", fail_observation_insert)
        try:
            with pytest.raises(RuntimeError, match="fault after"):
                repo.observe(
                    observation(
                        candidate(title="Bad partial update"), at=NOW + timedelta(seconds=1)
                    ),
                    (),
                )
        finally:
            event.remove(migrated_engine, "before_cursor_execute", fail_observation_insert)
        assert repo.get(first.tender.internal_id) == first.tender
        assert len(repo.history(first.tender.internal_id)) == 1
        assert len(repo.observations(first.tender.internal_id)) == 1
    with Session(migrated_engine) as session:
        assert SqlTenderRepository(session).get(first.tender.internal_id) == first.tender


def test_concurrent_same_observation_commits_exactly_once(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
    barrier = Barrier(2, timeout=10)
    seen = observation()

    def write() -> bool:
        with Session(migrated_engine) as session, session.begin():
            barrier.wait()
            return SqlTenderRepository(session).observe(seen, ()).replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        replays = list(executor.map(lambda _: write(), range(2)))
    assert sorted(replays) == [False, True]
    with Session(migrated_engine) as session:
        for model in (TenderRow, TenderVersionRow, TenderObservationRow):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_overlapping_batch_retry_cannot_deadlock_on_observation_locks(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
    first_written = Event()
    retry_waiting_for_source = Event()
    first_seen = observation()
    second_seen = observation(candidate(title="Changed"), at=NOW + timedelta(seconds=1))

    def notice_retry_lock(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if (
            current_thread().name.startswith("retry")
            and "FROM sources" in statement
            and "FOR UPDATE" in statement
        ):
            retry_waiting_for_source.set()

    def first_batch() -> None:
        with Session(migrated_engine) as session, session.begin():
            repo = SqlTenderRepository(session)
            repo.observe(first_seen, ())
            first_written.set()
            assert retry_waiting_for_source.wait(timeout=5)
            repo.observe(second_seen, ())

    def overlapping_retry() -> None:
        assert first_written.wait(timeout=5)
        with Session(migrated_engine) as session, session.begin():
            repo = SqlTenderRepository(session)
            assert repo.observe(second_seen, ()).replayed
            assert repo.observe(first_seen, ()).replayed

    event.listen(migrated_engine, "before_cursor_execute", notice_retry_lock)
    try:
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="first") as first_pool,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="retry") as retry_pool,
        ):
            first_future = first_pool.submit(first_batch)
            retry_future = retry_pool.submit(overlapping_retry)
            first_future.result(timeout=10)
            retry_future.result(timeout=10)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", notice_retry_lock)
    with Session(migrated_engine) as session:
        repo = SqlTenderRepository(session)
        tender = repo.for_source("s")[0]
        assert tender.title == "Changed"
        assert len(repo.history(tender.internal_id)) == 2
        assert len(repo.observations(tender.internal_id)) == 2


def test_missing_state_is_readable_replay_safe_and_reappearance_preserves_history(
    migrated_engine: Engine,
) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        seen = observation()
        first = repo.observe(seen, ())
        # Stand in for future accepted-run accounting; Phase 2 has no source-run processor.
        session.execute(
            update(TenderRow)
            .where(TenderRow.id == first.tender.internal_id)
            .values(missing_successful_runs=3, last_accounted_run_sequence=10)
        )
        missing = repo.get(first.tender.internal_id)
        assert missing is not None
        assert not ConfigurableTenderValidity().evaluate(missing, SourceSettings(), NOW).matchable
        replay = repo.observe(seen, ())
        assert replay.tender.presence.missing_successful_runs == 3
        fresh = repo.observe(observation(at=NOW + timedelta(days=1)), ())
        assert fresh.tender.presence.missing_successful_runs == 0
        assert fresh.tender.presence.last_accounted_run_sequence == 10
        assert len(repo.history(first.tender.internal_id)) == 1


def test_invalid_tender_keeps_current_state_and_history(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        first = repo.observe(observation(), ())
        closed = repo.observe(
            observation(candidate(status="cancelled", deadline=NOW), at=NOW + timedelta(seconds=1)),
            ("status",),
        )
        assert (
            not ConfigurableTenderValidity()
            .evaluate(closed.tender, SourceSettings(), NOW)
            .matchable
        )
        assert repo.get(first.tender.internal_id) == closed.tender
        assert len(repo.history(first.tender.internal_id)) == 2


@pytest.mark.parametrize("table", ["tender_versions", "tender_observations"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_audit_tables_are_append_only(migrated_engine: Engine, table: str, operation: str) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        SqlTenderRepository(session).observe(observation(), ())
    statement = (
        f"UPDATE {table} SET evidence_reference = 'changed'"
        if operation == "UPDATE"
        else f"DELETE FROM {table}"
    )
    with pytest.raises(DBAPIError):
        with migrated_engine.begin() as connection:
            connection.execute(text(statement))


def test_current_version_cannot_reference_another_tender(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        register(session)
        repo = SqlTenderRepository(session)
        first = repo.observe(observation(), ())
        second = repo.observe(observation(candidate(external_tender_id="002")), ())
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                update(TenderRow)
                .where(TenderRow.id == first.tender.internal_id)
                .values(current_version_id=second.tender.current_version_id)
            )


def test_quality_issues_and_raw_values_survive_database_roundtrip(migrated_engine: Engine) -> None:
    source = SourceRecord(id="s", name="Source")
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "001", "value": "bad-money"},
        source,
        SourceSettings.model_validate(
            {"normalization": {"parse_failure_policy": "null_with_issue"}}
        ),
    )
    assert result.candidate is not None
    with Session(migrated_engine) as session, session.begin():
        register(session)
        first = SqlTenderRepository(session).observe(observation(result.candidate), ())
    with Session(migrated_engine) as session:
        tender = SqlTenderRepository(session).get(first.tender.internal_id)
        assert tender is not None
        assert tender.value is None
        assert tender.raw_data["value"] == "bad-money"
        assert tender.quality_issues == result.issues
