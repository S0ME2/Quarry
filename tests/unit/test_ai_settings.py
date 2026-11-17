from collections.abc import Iterator
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from tender_system.core.configuration import ModelSettings
from tender_system.operator import ai_settings


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, Any]]:
    values: dict[str, Any] = {}
    monkeypatch.setenv("QUARRY_SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(ai_settings, "lock_registry", lambda session: None)
    monkeypatch.setattr(ai_settings, "get_setting", lambda session, key: values.get(key))
    monkeypatch.setattr(
        ai_settings, "set_setting", lambda session, key, value: values.update({key: value})
    )

    def database() -> Iterator[Session]:
        yield None  # type: ignore[misc]

    app = FastAPI()
    ai_settings.register_ai_settings(app, database)
    return TestClient(app), values


def test_registry_keeps_keys_encrypted_and_task_models_independent(
    registry: tuple[TestClient, dict[str, Any]],
) -> None:
    client, values = registry
    response = client.post(
        "/api/v1/admin/ai/providers",
        json={"name": "Example", "base_url": "https://example.org/v1", "api_key": "private-value"},
    )
    assert response.status_code == 201
    provider_id = response.json()["id"]
    assert "private-value" not in str(values)
    assert "private-value" not in client.get("/api/v1/admin/ai").text
    for role, model, limit in (
        ("recipe_generation", "large", 8000),
        ("tender_matching", "qwen", 500),
    ):
        assert (
            client.put(
                f"/api/v1/admin/ai/tasks/{role}",
                json={"provider_id": provider_id, "model": model, "max_output_tokens": limit},
            ).status_code
            == 200
        )
    fallback = ModelSettings(provider="groq", model="qwen")
    generation = ai_settings.assignment(None, "recipe_generation", fallback)  # type: ignore[arg-type]
    matching = ai_settings.assignment(None, "tender_matching", fallback)  # type: ignore[arg-type]
    assert (generation.model, generation.max_output_tokens) == ("large", 8000)
    assert (matching.model, matching.max_output_tokens) == ("qwen", 500)
    from tender_system.matching.model_providers import ChatCompletionsModelClient

    model_client = cast(
        ChatCompletionsModelClient, ai_settings.registry_client(cast(Session, None), generation)
    )
    assert model_client.endpoint == "https://example.org/v1/chat/completions"
    values["ai.providers"][provider_id]["base_url"] = "https://other.example/v1"
    with pytest.raises(ValueError, match="changed"):
        ai_settings.registry_client(cast(Session, None), generation)
    assert client.delete(f"/api/v1/admin/ai/providers/{provider_id}").status_code == 409


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://user:pass@example.org",
        "https://example.org:444",
        "https://example.org/?token=x",
    ],
)
def test_invalid_provider_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ai_settings.ProviderWrite(name="invalid", base_url=url)
