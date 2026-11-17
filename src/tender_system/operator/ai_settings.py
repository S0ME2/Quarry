"""Administrator AI registry and role assignments."""

import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, JsonValue, SecretStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tender_system.collection.network import PublicHTTPTransport
from tender_system.core.configuration import ModelSettings
from tender_system.core.ports import ModelClient
from tender_system.matching.model_providers import ChatCompletionsModelClient
from tender_system.persistence.admin_settings import (
    decrypt_secret,
    encrypt_secret,
    get_setting,
    set_setting,
)

Role = Literal["recipe_generation", "tender_matching", "extraction_verification"]


class ProviderWrite(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=2048)
    protocol: Literal["openai-compatible"] = "openai-compatible"
    enabled: bool = True
    api_key: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or url.port not in {None, 443}
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                "Use an HTTPS base URL without credentials, query, or custom port"
            ) from None
        return value.rstrip("/")


class TaskWrite(BaseModel):
    model_config = {"extra": "forbid"}
    provider_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    max_output_tokens: int = Field(ge=1, le=32768)
    max_input_bytes: int = Field(default=11000, ge=1, le=1000000)
    context_window_tokens: int = Field(default=32768, ge=1, le=2000000)
    timeout_seconds: int = Field(default=90, ge=1, le=300)


def assignment(session: Session, role: Role, fallback: ModelSettings) -> ModelSettings:
    raw = (get_setting(session, "ai.tasks") or {}).get(role)
    if not isinstance(raw, dict):
        return fallback
    task = TaskWrite.model_validate(raw)
    provider = (get_setting(session, "ai.providers") or {}).get(task.provider_id)
    if not isinstance(provider, dict) or not provider.get("enabled"):
        raise ValueError("Configured AI provider is unavailable")
    fingerprint = provider_fingerprint(provider)
    return fallback.model_copy(
        update={
            "provider": "registry/" + task.provider_id + "/" + fingerprint,
            "model": task.model,
            "max_output_tokens": task.max_output_tokens,
            "max_input_bytes": task.max_input_bytes,
            "context_window_tokens": task.context_window_tokens,
            "timeout_seconds": task.timeout_seconds,
            "max_attempts": 1,
            "api_key_env": None,
        }
    )


def provider_fingerprint(provider: dict[str, JsonValue]) -> str:
    return hashlib.sha256(
        json.dumps(provider, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def registry_client(session: Session, model: ModelSettings) -> ModelClient:
    provider_id, _, fingerprint = model.provider.removeprefix("registry/").partition("/")
    raw = (get_setting(session, "ai.providers") or {}).get(provider_id)
    if (
        not isinstance(raw, dict)
        or not raw.get("enabled")
        or provider_fingerprint(raw) != fingerprint
    ):
        raise ValueError("Configured AI provider changed or is unavailable")
    api_key = raw.get("encrypted_key")
    if not isinstance(api_key, str):
        raise ValueError("Configured AI provider needs a credential")
    url = ProviderWrite.model_validate(
        {k: v for k, v in raw.items() if k != "encrypted_key"}
    ).base_url
    client = ChatCompletionsModelClient(SecretStr(decrypt_secret(api_key)), PublicHTTPTransport())
    client.endpoint = url + "/chat/completions"
    client.settings = model
    return client


def lock_registry(session: Session) -> None:
    # Provider and assignment updates share one transaction lock to avoid lost writes.
    session.execute(select(func.pg_advisory_xact_lock(5189062794521012)))


def register_ai_settings(app: FastAPI, database: Callable[..., Iterator[Session]]) -> None:
    Database = Annotated[Session, Depends(database, scope="function")]

    @app.get("/api/v1/admin/ai")
    def status(session: Database) -> dict[str, JsonValue]:
        providers = get_setting(session, "ai.providers") or {}
        safe: dict[str, JsonValue] = {}
        for key, value in providers.items():
            if isinstance(value, dict):
                safe[key] = {k: v for k, v in value.items() if k != "encrypted_key"}
                cast(dict[str, JsonValue], safe[key])["has_api_key"] = bool(
                    value.get("encrypted_key")
                )
        return {"providers": safe, "tasks": get_setting(session, "ai.tasks") or {}}

    @app.post("/api/v1/admin/ai/providers", status_code=201)
    def add(body: ProviderWrite, session: Database) -> dict[str, str]:
        lock_registry(session)
        if body.api_key is None or not body.api_key.get_secret_value():
            raise HTTPException(422, "API key is required")
        key = uuid4().hex
        providers = get_setting(session, "ai.providers") or {}
        providers[key] = {
            **body.model_dump(mode="json", exclude={"api_key"}),
            "encrypted_key": encrypt_secret(body.api_key.get_secret_value()),
        }
        set_setting(session, "ai.providers", providers)
        return {"id": key}

    @app.put("/api/v1/admin/ai/providers/{provider_id}")
    def edit(provider_id: str, body: ProviderWrite, session: Database) -> dict[str, str]:
        lock_registry(session)
        providers = get_setting(session, "ai.providers") or {}
        old = providers.get(provider_id)
        if not isinstance(old, dict):
            raise HTTPException(404, "Provider unavailable")
        encrypted = old.get("encrypted_key")
        if body.api_key is not None:
            if not body.api_key.get_secret_value():
                raise HTTPException(422, "API key cannot be empty")
            encrypted = encrypt_secret(body.api_key.get_secret_value())
        providers[provider_id] = {
            **body.model_dump(mode="json", exclude={"api_key"}),
            "encrypted_key": encrypted,
        }
        set_setting(session, "ai.providers", providers)
        return {"id": provider_id}

    @app.delete("/api/v1/admin/ai/providers/{provider_id}")
    def remove(provider_id: str, session: Database) -> dict[str, bool]:
        lock_registry(session)
        providers = get_setting(session, "ai.providers") or {}
        if provider_id not in providers:
            raise HTTPException(404, "Provider unavailable")
        if any(
            isinstance(item, dict) and item.get("provider_id") == provider_id
            for item in (get_setting(session, "ai.tasks") or {}).values()
        ):
            raise HTTPException(409, "Provider is assigned to a task")
        del providers[provider_id]
        set_setting(session, "ai.providers", providers)
        return {"deleted": True}

    @app.put("/api/v1/admin/ai/tasks/{role}")
    def assign(role: Role, body: TaskWrite, session: Database) -> dict[str, str]:
        lock_registry(session)
        provider = (get_setting(session, "ai.providers") or {}).get(body.provider_id)
        if not isinstance(provider, dict) or not provider.get("enabled"):
            raise HTTPException(422, "An enabled provider is required")
        tasks = get_setting(session, "ai.tasks") or {}
        tasks[role] = body.model_dump(mode="json")
        set_setting(session, "ai.tasks", tasks)
        return {"role": role}

    @app.post("/api/v1/admin/ai/providers/{provider_id}/test")
    def test(provider_id: str, session: Database) -> dict[str, bool]:
        raw = (get_setting(session, "ai.providers") or {}).get(provider_id)
        if not isinstance(raw, dict) or not raw.get("enabled"):
            raise HTTPException(404, "Provider unavailable")
        try:
            url = ProviderWrite.model_validate(
                {k: v for k, v in raw.items() if k != "encrypted_key"}
            ).base_url
            key = decrypt_secret(str(raw["encrypted_key"]))
            with httpx.Client(
                transport=PublicHTTPTransport(), timeout=10, follow_redirects=False
            ) as client:
                response = client.get(url + "/models", headers={"Authorization": "Bearer " + key})
            return {"ok": response.is_success}
        except (ValueError, httpx.HTTPError, KeyError):
            return {"ok": False}
