"""Single-user production startup needs no application credentials."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tender_system.api.server import app_factory
from tender_system.core.environment import startup_settings
from tender_system.persistence.storage import OperatorConfigRow, session_factory


@pytest.mark.database
def test_production_starts_without_identity(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tender_system.api.server as server

    monkeypatch.setattr(server, "build_engine", lambda _: migrated_engine)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture:fixture@127.0.0.1/fixture")
    for key in (
        "QUARRY_OPERATOR_TOKEN",
        "QUARRY_COMPANY_TOKEN",
        "QUARRY_IDENTITY_FILE",
        "QUARRY_COMPANY_ID",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("QUARRY_REGISTER_CATALOG", "false")
    monkeypatch.setenv("QUARRY_OPERATOR_WORKER_ENABLED", "false")
    with TestClient(app_factory()) as client:
        for path in ("sources", "tenders", "campaigns", "activity"):
            assert client.get("/api/v1/web/" + path).status_code == 200
        assert client.get("/api/v1/identity").status_code == 404
        assert "securitySchemes" not in client.get("/openapi.json").json().get("components", {})
    with session_factory(migrated_engine)() as session:
        assert session.get(OperatorConfigRow, "identity") is None


def test_binding_stays_local() -> None:
    assert startup_settings({}).api_host == "127.0.0.1"
    for host in ("0.0.0.0", "::", "192.168.1.10"):
        with pytest.raises(ValueError):
            startup_settings({"QUARRY_API_HOST": host})


@pytest.mark.parametrize("database_failure", [False, True])
def test_startup_failure_explains_recovery_without_credentials(
    monkeypatch: pytest.MonkeyPatch, database_failure: bool
) -> None:
    from sqlalchemy.exc import OperationalError

    from tender_system.api import server

    def unavailable() -> FastAPI:
        if database_failure:
            raise OperationalError("private query", {}, Exception("private credential"))
        raise ValueError("private credential")

    monkeypatch.setattr(server, "app_factory", unavailable)
    with pytest.raises(SystemExit) as failure:
        server.main()
    message = str(failure.value)
    assert "private" not in message
    assert ".env" in message if not database_failure else "DATABASE_REBUILD.md" in message
