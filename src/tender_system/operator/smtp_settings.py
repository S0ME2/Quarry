"""Administrator SMTP configuration backed by encrypted application settings."""

import os
import re
import smtplib
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy.orm import Session, sessionmaker

from tender_system.core.domain import NotificationSettings
from tender_system.operator.email_delivery import EmailConfigurationError, send_email
from tender_system.persistence.admin_settings import (
    decrypt_secret,
    encrypt_secret,
    get_setting,
    set_setting,
)

KEY = "smtp.config"


class SmtpWrite(BaseModel):
    model_config = {"extra": "forbid"}
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    security: Literal["starttls", "tls"]
    username: str = Field(default="", max_length=256)
    password: SecretStr | None = None
    sender: str = Field(min_length=3, max_length=254)

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9.:-]+", value):
            raise ValueError("Invalid SMTP host")
        return value

    @field_validator("sender")
    @classmethod
    def valid_sender(cls, value: str) -> str:
        NotificationSettings(enabled=False, email=value)
        return value


class TestEmail(BaseModel):
    model_config = {"extra": "forbid"}
    destination: str = Field(min_length=3, max_length=254)

    @field_validator("destination")
    @classmethod
    def valid_destination(cls, value: str) -> str:
        NotificationSettings(enabled=False, email=value)
        return value


def delivery_config(session: Session) -> dict[str, Any] | None:
    value = get_setting(session, KEY)
    if value is None:
        return None
    result: dict[str, Any] = dict(value)
    secret = result.pop("password_encrypted", None)
    result["password"] = decrypt_secret(str(secret)) if secret else ""
    return result


def register_smtp(app: FastAPI, sessions: sessionmaker[Session]) -> None:
    # API middleware requires an administrator outside trusted local operator mode.
    def database() -> Iterator[Session]:
        with sessions.begin() as session:
            yield session

    Database = Annotated[Session, Depends(database, scope="function")]

    @app.get("/api/v1/admin/smtp")
    def smtp_status(session: Database) -> dict[str, Any]:
        value = get_setting(session, KEY)
        if value is None:
            return {
                "configured": bool(os.getenv("QUARRY_SMTP_HOST") and os.getenv("QUARRY_SMTP_FROM")),
                "source": "environment",
            }
        return {
            "configured": True,
            "source": "settings",
            "host": value.get("host"),
            "port": value.get("port"),
            "security": value.get("security"),
            "username": value.get("username"),
            "sender": value.get("sender"),
            "password_configured": bool(value.get("password_encrypted")),
        }

    @app.put("/api/v1/admin/smtp")
    def smtp_update(body: SmtpWrite, session: Database) -> dict[str, bool]:
        previous = get_setting(session, KEY) or {}
        secret = previous.get("password_encrypted")
        if body.password is not None:
            password = body.password.get_secret_value()
            secret = encrypt_secret(password) if password else None
        set_setting(
            session,
            KEY,
            {
                "host": body.host,
                "port": body.port,
                "security": body.security,
                "username": body.username,
                "sender": body.sender,
                "password_encrypted": secret,
            },
        )
        return {"configured": True}

    @app.post("/api/v1/admin/smtp/test")
    def smtp_test(body: TestEmail, session: Database) -> dict[str, str]:
        try:
            config = delivery_config(session)
            send_email(body.destination, "Quarry SMTP test message.", config)
        except EmailConfigurationError:
            raise HTTPException(409, "Email transport is not configured correctly") from None
        except (OSError, smtplib.SMTPException):
            # A transport interruption may occur after server acceptance. Never retry.
            raise HTTPException(502, "Email was rejected or delivery outcome is unknown") from None
        return {"status": "accepted"}
