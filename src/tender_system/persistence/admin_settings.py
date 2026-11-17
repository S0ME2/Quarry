"""Persistent administrator settings; callers own authorization and transactions."""

import os
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from pydantic import JsonValue
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tender_system.persistence.storage import OperatorConfigRow


def get_setting(session: Session, key: str) -> dict[str, JsonValue] | None:
    row = session.get(OperatorConfigRow, key, populate_existing=True)
    return dict(row.value) if row is not None else None


def set_setting(session: Session, key: str, value: dict[str, JsonValue]) -> None:
    now = datetime.now(UTC)
    session.execute(
        insert(OperatorConfigRow)
        .values(key=key, value=value, updated_at=now)
        .on_conflict_do_update(
            index_elements=[OperatorConfigRow.key], set_={"value": value, "updated_at": now}
        )
    )


def _cipher() -> Fernet:
    key = os.environ.get("QUARRY_SETTINGS_ENCRYPTION_KEY", "")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise ValueError("A valid persistent QUARRY_SETTINGS_ENCRYPTION_KEY is required") from None


def encrypt_secret(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return _cipher().decrypt(value.encode("ascii")).decode()
    except (InvalidToken, UnicodeError):
        raise ValueError("Cannot decrypt administrator setting") from None
