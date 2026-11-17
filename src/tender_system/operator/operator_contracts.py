"""Workspace source, tender and operation contracts."""

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import Field, JsonValue, field_validator

from tender_system.core.configuration import SourceOverride
from tender_system.core.domain import Contract


class SourceWrite(Contract):
    enabled: bool | None = None
    manual_only: bool = False
    configuration: SourceOverride | None = None

    @field_validator("configuration")
    @classmethod
    def no_secret_configuration(cls, value: SourceOverride | None) -> SourceOverride | None:
        if value:
            data = value.model_dump(exclude_unset=True)
            if "authorized_session_reference" in data or any(
                "api_key_env" in data.get(key, {})
                for key in ("fixer_model", "stronger_fixer_model")
            ):
                raise ValueError("credential references require trusted provisioning")
        return value


class OperationView(Contract):
    id: UUID
    kind: str
    source_id: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    result: dict[str, JsonValue]


class OperatorDocument(Contract):
    data: dict[str, JsonValue]


class ManualTender(Contract):
    request_id: UUID
    title: str = Field(min_length=1, max_length=1000)
    description: str | None = Field(default=None, max_length=20000)
    buyer: str | None = None
    location: str | None = None
    value: str | None = None
    currency: str | None = None
    deadline: str | None = None
    source_url: str | None = None
    status: str = "open"
    product_items: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=100)


class SourceURL(Contract):
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
        ):
            raise ValueError("use an HTTP URL without embedded credentials")
        if any(
            re.search(r"token|secret|password|api.?key|signature|credential", key, re.I)
            for key, _ in parse_qsl(parsed.query)
        ):
            raise ValueError("use a source URL without secret-bearing query strings")
        return value
