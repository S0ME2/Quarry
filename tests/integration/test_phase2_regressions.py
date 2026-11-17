from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg.errors import UniqueViolation
from pydantic import JsonValue, ValidationError
from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tender_system.collection.normalization import (
    ConfigurableTenderNormalizer,
    ConservativeUrlCanonicalizer,
)
from tender_system.core.configuration import Configuration, IdentitySettings, SourceSettings
from tender_system.core.domain import (
    SourceRecord,
    TenderCandidate,
    TenderComparison,
    TenderIdentity,
    TenderObservation,
)
from tender_system.core.tenders import ConfigurableTenderValidity, reconstruct_history, state_of
from tender_system.persistence.storage import SqlSourceRepository, SqlTenderRepository, TenderRow


@pytest.mark.parametrize("numeric", [0, 1e20, 1e-10])
def test_numeric_json_money_is_not_treated_as_locale_text(numeric: int | float) -> None:
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "1", "value": numeric},
        SourceRecord(id="s", name="Source"),
        SourceSettings(),
    )
    assert result.candidate is not None
    assert result.candidate.value == Decimal(str(numeric))


def test_partial_date_formats_are_rejected_rather_than_defaulting_to_1900() -> None:
    with pytest.raises(ValidationError, match="complete calendar date"):
        Configuration.model_validate(
            {"sources": {"s": {"normalization": {"date_formats": {"deadline": ["%H:%M"]}}}}}
        )


def test_tender_contract_does_not_coerce_numeric_dates_into_epoch_instants() -> None:
    with pytest.raises(ValidationError):
        TenderCandidate(source_id="s", external_tender_id="1", deadline=0)  # type: ignore[arg-type]


def test_explicit_null_external_id_token_does_not_become_an_identity() -> None:
    result = ConfigurableTenderNormalizer().normalize(
        {"external_tender_id": "N/A"},
        SourceRecord(id="s", name="Source"),
        SourceSettings.model_validate({"normalization": {"null_values": ["N/A"]}}),
    )
    assert result.candidate is None
    assert result.issues[0].code == "identity_failure"


def test_source_without_stable_ids_cannot_claim_an_unstable_external_id() -> None:
    with pytest.raises(ValidationError):
        TenderIdentity(
            source_id="s", source_has_stable_external_id=False, external_tender_id="unstable"
        )


def test_url_rules_do_not_rewrite_other_parts_or_empty_query_marker() -> None:
    url = "HTTPS://Source.test/A%2fb?#fragment"
    assert (
        ConservativeUrlCanonicalizer().canonicalize(url, IdentitySettings(remove_fragment=True))
        == "HTTPS://Source.test/A%2fb?"
    )
    assert (
        ConservativeUrlCanonicalizer().canonicalize(
            url, IdentitySettings(drop_query_parameters=("unused",))
        )
        == url
    )
    with pytest.raises(ValueError):
        ConservativeUrlCanonicalizer().canonicalize(
            "https://source.test/path\npart", IdentitySettings()
        )


def test_url_identity_does_not_apply_unicode_text_normalization_to_paths() -> None:
    url = "https://source.test/cafe\u0301?ref=001#section"
    result = ConfigurableTenderNormalizer().normalize(
        {"source_url": url},
        SourceRecord(id="s", name="Source", has_stable_external_id=False),
        SourceSettings.model_validate({"identity": {"url_is_reliable": True}}),
    )
    assert result.candidate is not None
    assert result.candidate.source_url == url
    assert result.candidate.fallback_key == url


@pytest.mark.database
def test_normalize_persist_reconstruct_and_check_validity(migrated_engine: Engine) -> None:
    source = SourceRecord(id="s", name="Source")
    settings = SourceSettings.model_validate(
        {
            "timezone": "Asia/Tashkent",
            "normalization": {
                "field_mapping": {"external_tender_id": "id"},
                "thousands_separator": ",",
                "money_prefixes": ["USD"],
            },
        }
    )
    normalizer = ConfigurableTenderNormalizer()
    raws: tuple[dict[str, JsonValue], ...] = (
        {"id": "001", "title": "Equipment", "deadline": "2026-10-05", "value": "USD 1,234.50"},
        {"id": "001", "title": "Equipment", "deadline": None, "value": None},
    )
    with Session(migrated_engine) as session, session.begin():
        SqlSourceRepository(session).add(source)
        repo = SqlTenderRepository(session)
        first_id = None
        for day, raw in enumerate(raws, start=4):
            result = normalizer.normalize(raw, source, settings)
            assert result.candidate is not None
            saved = repo.observe(
                TenderObservation(
                    id=uuid4(),
                    candidate=result.candidate,
                    observed_at=datetime(2026, 10, day, tzinfo=UTC),
                    evidence_reference=f"fixture:day-{day}",
                ),
                settings.rematch_fields,
            )
            if first_id is None:
                first_id = saved.tender.internal_id
            assert first_id == saved.tender.internal_id
            assert reconstruct_history(repo.history(first_id)) == state_of(saved.tender)
            assert saved.tender.raw_data == raw
            assert saved.comparison.requires_rematch
            assert (
                ConfigurableTenderValidity()
                .evaluate(saved.tender, settings, datetime(2026, 10, day, tzinfo=UTC))
                .matchable
            )
        assert first_id is not None
        assert repo.history(first_id)[1].changed_fields == {"deadline": None, "value": None}


@pytest.mark.database
def test_bad_comparator_cannot_write_corrupt_history(migrated_engine: Engine) -> None:
    class BadComparator:
        def compare(
            self, stored: object, candidate: TenderCandidate, rematch_fields: tuple[str, ...]
        ) -> TenderComparison:
            return TenderComparison(
                classification="new",
                changed_fields={"title": "not full state"},
                requires_rematch=True,
            )

    with Session(migrated_engine) as session, session.begin():
        SqlSourceRepository(session).add(SourceRecord(id="s", name="Source"))
        repo = SqlTenderRepository(session, BadComparator())
        with pytest.raises(ValueError, match="full initial state"):
            repo.observe(
                TenderObservation(
                    id=uuid4(),
                    candidate=TenderCandidate(source_id="s", external_tender_id="1"),
                    observed_at=datetime(2026, 10, 4, tzinfo=UTC),
                    evidence_reference="fixture",
                ),
                (),
            )
        assert repo.for_source("s") == ()


@pytest.mark.database
def test_database_enforces_external_identity_uniqueness(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        SqlSourceRepository(session).add(SourceRecord(id="s", name="Source"))
        repo = SqlTenderRepository(session)
        saved = repo.observe(
            TenderObservation(
                id=uuid4(),
                candidate=TenderCandidate(source_id="s", external_tender_id="1"),
                observed_at=datetime(2026, 10, 4, tzinfo=UTC),
                evidence_reference="fixture",
            ),
            (),
        )
        row = (
            session.execute(
                select(TenderRow.__table__).where(TenderRow.id == saved.tender.internal_id)
            )
            .mappings()
            .one()
        )
        duplicate = {**dict(row), "id": uuid4()}
        with pytest.raises(IntegrityError) as error:
            with session.begin_nested():
                session.execute(insert(TenderRow).values(**duplicate))
        assert isinstance(error.value.orig, UniqueViolation)
        assert error.value.orig.diag.constraint_name == "uq_tenders_external_identity"
