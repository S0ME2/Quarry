"""Fresh schema contract, immutable evidence, and explicit legacy rejection."""

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.util import CommandError
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError

from tender_system.persistence.storage import Base

pytestmark = pytest.mark.database

AUDIT_TABLES = (
    "tender_versions",
    "tender_observations",
    "campaign_criteria",
    "match_results",
    "recipe_revisions",
    "source_runs",
)


def test_forward_upgrade_is_repeatable_and_matches_metadata(
    db_engine: Engine, migration_config: Config
) -> None:
    with db_engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        command.upgrade(migration_config, "head")
        command.upgrade(migration_config, "head")
        assert MigrationContext.configure(connection).get_current_revision() == "0005_accounts"
        assert set(inspect(connection).get_table_names()) == {
            *Base.metadata.tables,
            "alembic_version",
        }
        for table in ("settings", "operations"):
            assert "actor_id" not in {c["name"] for c in inspect(connection).get_columns(table)}
        assert "uq_match_result_feedback_owner" not in {
            c["name"] for c in inspect(connection).get_unique_constraints("match_results")
        }
        command.check(migration_config)


def test_downgrade_and_reupgrade(db_engine: Engine, migration_config: Config) -> None:
    with db_engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        command.upgrade(migration_config, "head")
        command.downgrade(migration_config, "base")
        assert "sources" not in inspect(connection).get_table_names()
        assert MigrationContext.configure(connection).get_current_revision() is None
        command.upgrade(migration_config, "head")
        command.check(migration_config)


def test_legacy_database_requires_explicit_rebuild(
    db_engine: Engine, migration_config: Config
) -> None:
    with db_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        )
        connection.execute(text("INSERT INTO alembic_version VALUES ('0013_matching_spend')"))
        migration_config.attributes["connection"] = connection
        with pytest.raises(CommandError, match="0013_matching_spend"):
            command.upgrade(migration_config, "head")
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_audit_tables_cannot_be_truncated(migrated_engine: Engine, table: str) -> None:
    with migrated_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
            connection.execute(text(f"TRUNCATE {table} CASCADE"))


@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_audit_mutation_triggers_cover_update_and_delete(
    migrated_engine: Engine, table: str
) -> None:
    with migrated_engine.connect() as connection:
        # PostgreSQL tgtype: ROW=1, BEFORE=2, DELETE=8, UPDATE=16.
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE tgrelid = CAST(:table AS regclass) "
                    "AND NOT tgisinternal AND tgtype = 27 "
                    "AND tgfoid = 'reject_audit_mutation()'::regprocedure"
                ),
                {"table": table},
            )
            == 1
        )


@pytest.mark.parametrize("table", ("source_runs", "recipe_revisions"))
@pytest.mark.parametrize("mutation", ("UPDATE", "DELETE"))
def test_source_evidence_is_append_only(migrated_engine: Engine, table: str, mutation: str) -> None:
    with migrated_engine.begin() as connection:
        connection.execute(text("INSERT INTO sources VALUES ('fixture', 'Fixture', true)"))
        if table == "source_runs":
            connection.execute(
                text(
                    "INSERT INTO source_runs VALUES "
                    "('00000000-0000-0000-0000-000000000001', 'fixture', 'failed', NULL, '{}')"
                )
            )
            column = "receipt"
        else:
            connection.execute(
                text("INSERT INTO recipe_revisions VALUES ('fixture', '1', '{}', now())")
            )
            column = "revision"
        statement = (
            f"UPDATE {table} SET {column} = '{{}}'"
            if mutation == "UPDATE"
            else f"DELETE FROM {table}"
        )
        with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
            connection.execute(text(statement))
