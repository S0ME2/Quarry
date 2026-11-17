from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tender_system.core.domain import SourceRecord
from tender_system.core.ports import SourceAlreadyExists, SourceRepository
from tender_system.persistence.storage import (
    SqlSourceRepository,
    build_engine,
    database_url_from_environment,
    session_factory,
)
from tender_system.testing.fakes import FakeSourceRepository


def source_repository_contract(repository: SourceRepository) -> None:
    source = SourceRecord(id="uz-source", name="Закупки — Toshkent", has_stable_external_id=False)
    assert repository.get("uz-source") is None
    repository.add(source)
    assert repository.get("uz-source") == source
    with pytest.raises(SourceAlreadyExists):
        repository.add(source)
    # Duplicate failure must not discard other work or poison the transaction.
    repository.add(SourceRecord(id="another", name="Another"))
    assert repository.get("another") is not None
    assert repository.get("uz-source") == source
    assert repository.get("missing") is None


def test_fake_source_repository_contract() -> None:
    source_repository_contract(FakeSourceRepository())


def test_fake_repositories_are_isolated() -> None:
    first = FakeSourceRepository()
    first.add(SourceRecord(id="s", name="Source"))
    assert FakeSourceRepository().get("s") is None


@pytest.mark.database
def test_sql_source_repository_contract(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session, session.begin():
        source_repository_contract(SqlSourceRepository(session))


@pytest.mark.database
def test_caller_controls_commit_and_rollback(migrated_engine: Engine) -> None:
    factory = session_factory(migrated_engine)
    with pytest.raises(RuntimeError, match="interruption"):
        with factory.begin() as session:
            SqlSourceRepository(session).add(SourceRecord(id="rolled-back", name="Abort"))
            raise RuntimeError("interruption")
    with factory.begin() as session:
        repository = SqlSourceRepository(session)
        assert repository.get("rolled-back") is None
        repository.add(SourceRecord(id="committed", name="Persist"))
    with factory() as session:
        assert SqlSourceRepository(session).get("committed") == SourceRecord(
            id="committed", name="Persist"
        )


@pytest.mark.database
def test_concurrent_duplicate_insertion_has_one_winner(migrated_engine: Engine) -> None:
    barrier = Barrier(2, timeout=10)

    def insert() -> str:
        try:
            with Session(migrated_engine) as session, session.begin():
                barrier.wait()
                SqlSourceRepository(session).add(SourceRecord(id="same-id", name="Source"))
            return "inserted"
        except SourceAlreadyExists:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: insert(), range(2)))
    assert sorted(outcomes) == ["duplicate", "inserted"]


@pytest.mark.database
def test_database_constraints_are_not_only_python_validation(migrated_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sources (id, name, has_stable_external_id) "
                    "VALUES ('', 'Bad', true)"
                )
            )
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sources (id, name, has_stable_external_id) "
                    "VALUES ('s', NULL, true)"
                )
            )


def test_engine_rejects_other_database_implementations() -> None:
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        build_engine(SecretStr("sqlite://"))


def test_database_credentials_are_external_and_redacted() -> None:
    url = database_url_from_environment(environ={"DATABASE_URL": "postgresql://test-secret-value"})
    assert "test-secret-value" not in repr(url)
    with pytest.raises(ValueError, match="missing database URL"):
        database_url_from_environment(environ={})
    alternate = database_url_from_environment(
        "ALTERNATE_URL", {"ALTERNATE_URL": "postgresql://test"}
    )
    assert alternate.get_secret_value() == "postgresql://test"
