"""Database tests require PostgreSQL and own a unique schema per test."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateSchema, DropSchema

from tender_system.persistence.storage import (
    build_engine,
    database_url_from_environment,
    validate_test_database,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-golden",
        action="store_true",
        default=False,
        help="Explicitly authorize public-source/provider golden tests (never used by integrity)",
    )


def pytest_configure(config: pytest.Config) -> None:
    from tender_system.core.environment import load_environment

    explicit_live = (
        config.getoption("--live-golden") and os.environ.get("QUARRY_LIVE_GOLDEN") == "1"
    )
    explicit_telegram = os.environ.get("QUARRY_LIVE_TELEGRAM") == "1"
    load_environment()
    # Dotenv must never opt ordinary tests into public network/provider requests.
    if not explicit_live:
        os.environ["QUARRY_LIVE_GOLDEN"] = ""
    if not explicit_telegram:
        os.environ["QUARRY_LIVE_TELEGRAM"] = ""


@pytest.fixture(autouse=True)
def legacy_operator_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    # Existing operator tests explicitly opt into local operator mode. Account tests
    # compose create_app(operator_mode=False), independently of this setting.
    monkeypatch.setenv("QUARRY_OPERATOR_MODE", "true")


@pytest.fixture(autouse=True)
def isolate_telegram_credentials(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty values also prevent later dotenv loads from restoring real credentials.
    live_tests = {
        "test_live_telegram_persisted_matching_workflow",
        "test_existing_iom_recipe_scheduled_documents_matching_and_reuse",
    }
    if os.environ.get("QUARRY_LIVE_TELEGRAM") != "1" or request.node.name not in live_tests:
        monkeypatch.setenv("QUARRY_TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("QUARRY_TELEGRAM_CHAT_ID", "")


@pytest.fixture(autouse=True)
def isolate_email_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Normal regression tests must never deliver email through a developer's SMTP account.
    for key in ("HOST", "FROM", "USERNAME", "PASSWORD"):
        monkeypatch.setenv("QUARRY_SMTP_" + key, "")


@pytest.fixture(scope="session")
def postgres_url() -> SecretStr:
    try:
        url = database_url_from_environment("TEST_DATABASE_URL")
        validate_test_database(url.get_secret_value(), os.environ.get("DATABASE_URL"))
        return url
    except ValueError:
        raise pytest.UsageError(
            "Database tests require TEST_DATABASE_URL pointing to PostgreSQL. "
            "Use make test-unit for network-free tests."
        ) from None


@pytest.fixture
def db_engine(postgres_url: SecretStr) -> Iterator[Engine]:
    admin = build_engine(postgres_url)
    schema = f"test_{uuid4().hex}"
    try:
        with admin.connect() as connection:
            actual = connection.scalar(text("select current_database()"))
            if actual != admin.url.database or not actual or "test" not in actual.lower():
                admin.dispose()
                raise pytest.UsageError("Connected database is not the selected test database")
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
    except SQLAlchemyError:
        admin.dispose()
        pytest.fail(
            "Test PostgreSQL connection/schema setup failed; check TEST_DATABASE_URL, "
            "server availability and schema permissions. Credentials omitted.",
            pytrace=False,
        )
    url = admin.url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = build_engine(SecretStr(url.render_as_string(hide_password=False)))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.fixture
def migration_config() -> Config:
    root = Path(__file__).resolve().parents[1]
    return Config(str(root / "alembic.ini"))


@pytest.fixture
def migrated_engine(db_engine: Engine, migration_config: Config) -> Engine:
    with db_engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        command.upgrade(migration_config, "head")
    return db_engine
