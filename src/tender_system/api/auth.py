"""Opaque, revocable database sessions; no tokens or passwords in API projections."""

import hashlib
import hmac
import os
import secrets
from collections.abc import Iterator
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tender_system.core.clock import SystemClock
from tender_system.core.domain import Contract, NotificationSettings
from tender_system.core.ports import Clock
from tender_system.persistence.storage import UserRow, UserSessionRow

COOKIE = "quarry_session"


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + digest.hex()


def password_matches(password: str, encoded: str) -> bool:
    salt, _, _ = encoded.partition(":")
    return hmac.compare_digest(password_hash(password, salt), encoded)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Credentials(Contract):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        NotificationSettings(enabled=False, email=value)
        return value


class UserView(Contract):
    id: UUID
    email: str
    role: str
    active: bool
    operator: bool = False


def user_view(row: UserRow) -> UserView:
    return UserView(id=row.id, email=row.email, role=row.role, active=row.active)


def session_user(session: Session, request: Request, clock: Clock) -> UserRow | None:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 256:
        return None
    return session.scalar(
        select(UserRow)
        .join(UserSessionRow, UserSessionRow.user_id == UserRow.id)
        .where(
            UserSessionRow.token_hash == token_hash(token),
            UserSessionRow.expires_at > clock.now(),
            UserRow.active.is_(True),
        )
    )


def register_auth(
    app: FastAPI,
    sessions: sessionmaker[Session],
    clock: Clock | None = None,
    *,
    operator_mode: bool = False,
) -> Any:
    clock = clock or SystemClock()

    def database() -> Iterator[Session]:
        with sessions.begin() as session:
            yield session

    Database = Annotated[Session, Depends(database, scope="function")]

    def current_user(request: Request, session: Database) -> UserRow:
        user = session_user(session, request, clock)
        if user is None:
            raise HTTPException(401, "Sign in required")
        return user

    @app.post("/api/v1/auth/register", response_model=UserView, status_code=201)
    def register(body: Credentials, session: Database) -> UserView:
        user = UserRow(
            id=uuid4(),
            email=body.email,
            password_hash=password_hash(body.password),
            role="user",
            active=True,
            created_at=clock.now(),
            failed_logins=0,
        )
        try:
            with session.begin_nested():
                session.add(user)
                session.flush()
        except IntegrityError:
            raise HTTPException(409, "Account cannot be registered") from None
        return user_view(user)

    @app.post("/api/v1/auth/login", response_model=UserView)
    def login(body: Credentials, request: Request, response: Response, session: Database) -> Any:
        user = session.scalar(select(UserRow).where(UserRow.email == body.email).with_for_update())
        now = clock.now()
        if user and user.locked_until and user.locked_until > now:
            raise HTTPException(401, "Invalid credentials or temporarily unavailable account")
        valid = (
            password_matches(body.password, user.password_hash)
            if user
            else (password_matches(body.password, "0" * 32 + ":" + "0" * 128))
        )
        if not user or not user.active or not valid:
            if user and user.active:
                user.failed_logins += 1
                if user.failed_logins >= 5:
                    user.locked_until = now + timedelta(minutes=15)
                session.commit()  # Persist lockout despite rejected login.
            raise HTTPException(401, "Invalid credentials or temporarily unavailable account")
        user.failed_logins, user.locked_until = 0, None
        old = request.cookies.get(COOKIE)
        if old:
            session.execute(
                delete(UserSessionRow).where(UserSessionRow.token_hash == token_hash(old))
            )
        session.execute(delete(UserSessionRow).where(UserSessionRow.expires_at <= now))
        token = secrets.token_urlsafe(48)
        session.add(
            UserSessionRow(
                token_hash=token_hash(token), user_id=user.id, expires_at=now + timedelta(hours=12)
            )
        )
        response.set_cookie(
            COOKIE,
            token,
            max_age=43200,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https" or os.getenv("QUARRY_SECURE_COOKIES") == "true",
            path="/",
        )
        return user_view(user)

    @app.get("/api/v1/auth/me", response_model=UserView)
    def me(request: Request, session: Database) -> UserView:
        if operator_mode:
            return UserView(
                id=UUID(int=0), email="operator@localhost", role="admin", active=True, operator=True
            )
        user = session_user(session, request, clock)
        if user is None:
            raise HTTPException(401, "Sign in required")
        return user_view(user)

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(request: Request, response: Response, session: Database) -> None:
        token = request.cookies.get(COOKIE)
        if token:
            session.execute(
                delete(UserSessionRow).where(UserSessionRow.token_hash == token_hash(token))
            )
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")

    return database, current_user
