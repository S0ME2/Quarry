"""Administrator settings survive independent sessions without storing plaintext."""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tender_system.persistence.admin_settings import (
    decrypt_secret,
    encrypt_secret,
    get_setting,
    set_setting,
)

pytestmark = pytest.mark.database


def test_persistent_encrypted_setting(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARRY_SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with Session(migrated_engine) as session:
        set_setting(session, "test.provider", {"secret": encrypt_secret("sensitive value")})
        session.commit()
    with Session(migrated_engine) as session:
        value = get_setting(session, "test.provider")
        assert value is not None
        assert isinstance(value["secret"], str)
        assert "sensitive value" not in value["secret"]
        assert decrypt_secret(value["secret"]) == "sensitive value"
        set_setting(session, "test.provider", {"secret": encrypt_secret("updated")})
        session.commit()
    with Session(migrated_engine) as session:
        value = get_setting(session, "test.provider")
        assert value is not None and isinstance(value["secret"], str)
        assert decrypt_secret(value["secret"]) == "updated"


def test_ai_registry_persists_and_hides_credentials(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from tender_system.api.api import create_app
    from tender_system.persistence.storage import session_factory

    monkeypatch.setenv("QUARRY_SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    sessions = session_factory(migrated_engine)
    with TestClient(create_app(sessions, operator_mode=True)) as client:
        response = client.post(
            "/api/v1/admin/ai/providers",
            json={
                "name": "Example",
                "base_url": "https://example.org/v1",
                "api_key": "secret-value",
            },
        )
        assert response.status_code == 201
        provider_id = response.json()["id"]
        assert (
            client.put(
                "/api/v1/admin/ai/tasks/recipe_generation",
                json={"provider_id": provider_id, "model": "example", "max_output_tokens": 4096},
            ).status_code
            == 200
        )
    with TestClient(create_app(sessions, operator_mode=True)) as client:
        status = client.get("/api/v1/admin/ai")
        assert status.status_code == 200
        assert "secret-value" not in status.text
        assert status.json()["tasks"]["recipe_generation"]["model"] == "example"
        assert status.json()["providers"][provider_id]["has_api_key"] is True
