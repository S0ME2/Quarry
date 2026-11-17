"""Operational precedence against isolated test schemas."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine

from tender_system.core.configuration import load_configuration
from tender_system.operator.operations import OperatorService, effective_configuration
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import (
    session_factory,
)

NOW = datetime(2026, 10, 7, 9, tzinfo=UTC)

pytestmark = pytest.mark.database


def test_persisted_source_updates_preserve_unrelated_values_and_safety(
    migrated_engine: Engine,
) -> None:
    from tender_system.core.configuration import SourceOverride
    from tender_system.core.domain import SourceRecord
    from tender_system.operator.operator_contracts import SourceWrite
    from tender_system.persistence.storage import SqlSourceRepository

    sessions = session_factory(migrated_engine)
    config = load_configuration(environ={"QUARRY_MATCHER__MAX_OUTPUT_TOKENS": "321"})
    with sessions.begin() as session:
        SqlSourceRepository(session).add(SourceRecord(id="fixture", name="Fixture"))
        svc = OperatorService(session, config, NOW)
        svc.configure_source(
            "fixture", SourceWrite(configuration=SourceOverride(timeout_seconds=50))
        )
        svc.configure_source("fixture", SourceWrite(enabled=False))
        effective = effective_configuration(session, config)
        assert effective.for_source("fixture").timeout_seconds == 50
        assert svc.control("fixture")["enabled"] is False
        assert effective.global_settings.campaign.model.max_output_tokens == 321
        assert effective.for_campaign("x").matcher_adapter == "rules"
        assert effective.for_source("x").repair.mode == "human"


def test_retired_ai_operations_fail_before_external_execution(migrated_engine: Engine) -> None:
    runtime = OperatorRuntime(session_factory(migrated_engine), load_configuration(environ={}))
    try:
        for kind in ("onboarding", "repair"):
            with pytest.raises(ValueError, match="unsupported operation"):
                runtime.execute(uuid4(), kind, "fixture", {}, runtime.config)
    finally:
        runtime.close()


def test_doctor_reports_pending_revision_without_changing_database(
    db_engine: Engine,
    migration_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from alembic import command
    from pydantic import SecretStr
    from sqlalchemy import text

    from tender_system.operator import manual_cli as cli

    with db_engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        command.upgrade(migration_config, "head")
        connection.execute(text("UPDATE alembic_version SET version_num='outdated'"))
    monkeypatch.setattr(
        cli,
        "database_url_from_environment",
        lambda _: SecretStr(db_engine.url.render_as_string(hide_password=False)),
    )
    assert cli.doctor(cli.parser().parse_args(["doctor"]), load_configuration(environ={})) == 1
    report = json.loads(capsys.readouterr().out)
    check = next(c for c in report["checks"] if c["check"] == "migrations")
    assert not check["passed"]
    assert check["current"] == ["outdated"]
    from alembic.script import ScriptDirectory

    assert check["required"] == list(ScriptDirectory.from_config(migration_config).get_heads())
    assert "does not apply" in check["action"]
    with db_engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == "outdated"
