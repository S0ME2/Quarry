"""Real PostgreSQL import safety, legacy evidence and atomic rollback."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import SourceRecord, TenderCandidate, TenderObservation
from tender_system.persistence.storage import (
    SqlSourceRepository,
    SqlTenderRepository,
    build_engine,
)
from tools.database_rebuild import TABLES, import_database

pytestmark = pytest.mark.database
NOW = datetime(2026, 10, 15, tzinfo=UTC)


@pytest.fixture
def legacy(migrated_engine: Engine) -> Engine:
    with Session(migrated_engine) as session, session.begin():
        SqlSourceRepository(session).add(SourceRecord(id="fixture", name="Fixture"))
        repository = SqlTenderRepository(session)
        for index in range(2):
            repository.observe(
                TenderObservation(
                    id=uuid4(),
                    observed_at=NOW + timedelta(minutes=index),
                    evidence_reference=f"fixture:page-{index}",
                    candidate=TenderCandidate(
                        source_id="fixture",
                        external_tender_id="1",
                        title=f"Router {index}",
                        raw_data={"original": index, "null": None},
                    ),
                ),
                SourceSettings().effective_rematch_fields,
            )
    with migrated_engine.begin() as connection:
        connection.execute(text("ALTER TABLE settings RENAME TO operator_config"))
        connection.execute(text("ALTER TABLE operations RENAME TO operator_operations"))
        connection.execute(text("ALTER TABLE operator_config ADD COLUMN actor_id text"))
        connection.execute(text("ALTER TABLE operator_operations ADD COLUMN actor_id text"))
        connection.execute(text("DROP TABLE matching_spend"))
        connection.execute(text("CREATE TABLE obsolete_feature (evidence jsonb NOT NULL)"))
        connection.execute(text("INSERT INTO obsolete_feature VALUES ('{\"archive\": true}')"))
        for key in (
            "source/fixture",
            "web/source/fixture",
            "model/matcher",
            "matching_budget",
            "system/identity",
            "model/fixer",
        ):
            connection.execute(
                text("INSERT INTO operator_config VALUES (:key, '{}', :now, 'old-actor')"),
                {"key": key, "now": NOW},
            )
    return migrated_engine


@pytest.fixture
def target(legacy: Engine, migration_config: Config) -> Iterator[Engine]:
    schema = "rebuild_" + uuid4().hex
    with legacy.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = build_engine(
        SecretStr(
            legacy.url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
                hide_password=False
            )
        )
    )
    try:
        with engine.begin() as connection:
            migration_config.attributes["connection"] = connection
            command.upgrade(migration_config, "head")
        yield engine
    finally:
        engine.dispose()
        with legacy.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


def test_import_preserves_exact_records_and_reconstructs_every_version(
    legacy: Engine,
    target: Engine,
) -> None:
    report = import_database(legacy, target)
    assert report["history"] == {
        "tenders": 1,
        "versions": 2,
        "observations": 2,
        "legacy_product_histories": 0,
    }
    assert report["tables"]["matching_spend"]["count"] == 0
    assert report["tables"]["settings"]["count"] == 4
    with Session(target) as session:
        repository = SqlTenderRepository(session)
        identifier: UUID = session.execute(text("SELECT id FROM tenders")).scalar_one()
        evidence = repository.observations(identifier)
        assert [item.candidate.title for item in evidence] == ["Router 0", "Router 1"]
        assert [item.candidate.raw_data for item in evidence] == [
            {"original": 0, "null": None},
            {"original": 1, "null": None},
        ]
    with legacy.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM operator_config")) == 6
        assert connection.scalar(text("SELECT count(*) FROM obsolete_feature")) == 1


def test_nonempty_target_rejected_without_modification(legacy: Engine, target: Engine) -> None:
    with target.begin() as connection:
        connection.execute(text("INSERT INTO settings VALUES ('existing', '{}', now())"))
    with pytest.raises(ValueError, match="must be empty"):
        import_database(legacy, target)
    with target.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tenders")) == 0
        assert connection.scalar(text("SELECT key FROM settings")) == "existing"


def test_history_failure_rolls_back_entire_import(legacy: Engine, target: Engine) -> None:
    with legacy.begin() as connection:
        connection.execute(text("UPDATE tenders SET title='inconsistent current state'"))
    with pytest.raises(ValueError, match="current fields disagree"):
        import_database(legacy, target)
    with target.connect() as connection:
        for name in TABLES:
            assert connection.scalar(text(f'SELECT count(*) FROM "{name}"')) == 0


def test_unexpected_target_schema_rejected(legacy: Engine, target: Engine) -> None:
    with target.begin() as connection:
        connection.execute(text("CREATE TABLE surprise (id integer)"))
    with pytest.raises(ValueError, match="current migrated schema"):
        import_database(legacy, target)


def test_pre_product_history_preserves_current_products_without_rewriting_root(
    legacy: Engine,
    target: Engine,
) -> None:
    with legacy.begin() as connection:
        # Recreate the genuine pre-product baseline shape found in old installations.
        connection.execute(text("DROP TRIGGER immutable_audit ON tender_versions"))
        connection.execute(
            text(
                "UPDATE tender_versions SET changed_fields=changed_fields - 'product_items' "
                "WHERE previous_version_id IS NULL"
            )
        )
        connection.execute(
            text("UPDATE tenders SET product_items=CAST(:payload AS jsonb)"),
            {"payload": '[{"name":"Router","quantity":2}]'},
        )
    report = import_database(legacy, target)
    assert report["history"]["legacy_product_histories"] == 1
    with target.connect() as connection:
        assert connection.scalar(text("SELECT product_items FROM tenders")) == [
            {"name": "Router", "quantity": 2},
        ]
        assert (
            connection.scalar(
                text(
                    "SELECT changed_fields ? 'product_items' FROM tender_versions "
                    "WHERE previous_version_id IS NULL"
                )
            )
            is False
        )


def test_same_database_schema_rejected_before_locking(migrated_engine: Engine) -> None:
    url = migrated_engine.url.update_query_dict(
        {"options": str(migrated_engine.url.query["options"]) + " -cstatement_timeout=500"}
    )
    alias = build_engine(SecretStr(url.render_as_string(hide_password=False)))
    try:
        with pytest.raises(ValueError, match="different database or schema"):
            import_database(alias, alias)
    finally:
        alias.dispose()


def test_later_product_diff_is_verified_even_with_legacy_root(
    legacy: Engine, target: Engine
) -> None:
    with legacy.begin() as connection:
        connection.execute(text("DROP TRIGGER immutable_audit ON tender_versions"))
        connection.execute(
            text(
                "UPDATE tender_versions SET changed_fields=changed_fields - 'product_items' "
                "WHERE previous_version_id IS NULL"
            )
        )
        connection.execute(
            text(
                "UPDATE tender_versions SET changed_fields=changed_fields || CAST(:diff AS jsonb) "
                "WHERE previous_version_id IS NOT NULL"
            ),
            {"diff": '{"product_items": [{"name": "Versioned router"}]}'},
        )
    with pytest.raises(ValueError, match="current fields disagree"):
        import_database(legacy, target)


def test_document_text_survives_database_rebuild(legacy: Engine, target: Engine) -> None:
    from tender_system.persistence.storage import TenderDocumentRow

    with Session(legacy) as session, session.begin():
        tender = SqlTenderRepository(session).for_source("fixture")[0]
        session.add(
            TenderDocumentRow(
                tender_id=tender.internal_id,
                url="https://example.org/spec.pdf",
                status="success",
                extracted_text="Public tender specifications",
                checked_at=NOW,
            )
        )
    report = import_database(legacy, target)
    assert report["tables"]["tender_documents"]["count"] == 1
    with Session(target) as session:
        tender = SqlTenderRepository(session).for_source("fixture")[0]
        document = session.get(
            TenderDocumentRow, (tender.internal_id, "https://example.org/spec.pdf")
        )
        assert document is not None
        assert document.extracted_text == "Public tender specifications"
