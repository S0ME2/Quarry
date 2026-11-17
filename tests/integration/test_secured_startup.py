"""Production composition serves built UI and enforces authentication by default."""

import time
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from tender_system.api import server
from tender_system.api.api import create_app
from tender_system.core.configuration import Configuration, load_configuration
from tender_system.operator.operator_runtime import OperatorRuntime
from tender_system.persistence.storage import session_factory

pytestmark = pytest.mark.database


def test_server_startup_static_registration_and_secured_api(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARRY_OPERATOR_MODE", "false")
    monkeypatch.setenv("QUARRY_REGISTER_CATALOG", "false")
    monkeypatch.setattr(server, "build_engine", lambda _: migrated_engine)
    config = load_configuration(environ={}, validate_models=False)
    monkeypatch.setattr(server, "load_configuration", lambda **_: config)
    with TestClient(server.app_factory()) as client:
        response = client.get("/")
        assert response.status_code == 200 and "<html" in response.text
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.get("/api/v1/web/sources").status_code == 401
        body = {"email": "startup@example.org", "password": "startup-fixture-password"}
        assert client.post("/api/v1/auth/register", json=body).status_code == 201
        assert client.post("/api/v1/auth/login", json=body).status_code == 200
        assert client.get("/api/v1/account/campaigns").json() == {"items": []}
        assert client.get("/api/v1/web/sources").status_code == 403
        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/account/campaigns").status_code == 401


def test_health_tracks_worker_failure_recovery_and_database(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = session_factory(migrated_engine)
    config = Configuration.model_validate(
        {"runtime": {"operator_worker_enabled": True, "worker_poll_seconds": 0.05}}
    )
    runtime = OperatorRuntime(sessions, config)
    failure = Event()

    def schedule() -> int:
        if failure.is_set():
            raise RuntimeError("private details")
        return 0

    monkeypatch.setattr(runtime, "schedule_due", schedule)
    monkeypatch.setattr(runtime, "tick", lambda: False)
    app = create_app(sessions, config, runtime=runtime, operator_mode=False)
    with TestClient(app) as client:

        def await_health(expected: int) -> None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                response = client.get("/health")
                if response.status_code == expected:
                    assert "private details" not in response.text
                    return
                time.sleep(0.05)
            pytest.fail(f"health did not reach {expected}")

        await_health(200)
        failure.set()
        await_health(503)
        failure.clear()
        await_health(200)
        runtime.stop.set()
        await_health(503)

    class BrokenSessions:
        def __call__(self) -> None:
            raise SQLAlchemyError("private database details")

    broken = create_app(BrokenSessions(), operator_mode=False)  # type: ignore[arg-type]
    with TestClient(broken) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["database"] is False
        assert "private database details" not in response.text
