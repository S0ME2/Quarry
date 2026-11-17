"""Real persisted matches and durable email delivery; no external SMTP traffic."""

import smtplib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from test_automatic_matching import campaign, collect, source

from tender_system.operator import telegram
from tender_system.operator.email_delivery import EmailConfigurationError
from tender_system.persistence.storage import MatchResultRow, TelegramNotificationRow
from tender_system.testing.web import web_fixture

pytestmark = pytest.mark.database


def test_email_is_after_commit_scoped_and_deduplicated(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, notifications={"channel": "email", "email": "buyer@example.org"})

        def send(destination: str, message: str) -> None:
            with fixture.runtime.sessions() as session:
                row = session.scalar(
                    select(TelegramNotificationRow).where(
                        TelegramNotificationRow.message == message
                    )
                )
                assert row is not None and row.status == "uncertain"
                assert row.channel == "email" and row.destination == destination
                assert session.get(MatchResultRow, row.result_id) is not None
            assert destination == "buyer@example.org"
            for field in (
                "Tender",
                "Buyer",
                "Price",
                "Currency",
                "Deadline",
                "Platform",
                "Relevance",
                "Link",
            ):
                assert field + ": " in message
            sent.append(destination)

        monkeypatch.setattr(telegram, "send_email", send)
        key = source(client, fixture)
        collect(client, key)
        assert sent == ["buyer@example.org"] * 2
        collect(client, key)
        assert len(sent) == 2
        with fixture.runtime.sessions() as session:
            assert all(
                row.status == "delivered"
                for row in session.scalars(select(TelegramNotificationRow))
            )


@pytest.mark.parametrize("failure", ["rejected", "connection", "configuration"])
def test_email_failures_preserve_results_and_retry_only_confirmed_rejections(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls: list[str] = []
    with web_fixture(migrated_engine) as fixture, TestClient(fixture.app) as client:
        campaign(client, notifications={"channel": "email", "email": "buyer@example.org"})

        def fail(destination: str, message: str) -> None:
            calls.append(destination)
            if failure == "rejected":
                raise smtplib.SMTPDataError(451, b"provider-private-diagnostics")
            if failure == "configuration":
                raise EmailConfigurationError("private-details")
            raise OSError("private-details")

        monkeypatch.setattr(telegram, "send_email", fail)
        collect(client, source(client, fixture))
        with fixture.runtime.sessions() as session:
            rows = list(session.scalars(select(TelegramNotificationRow)))
            assert len(rows) == 2
            assert all(
                row.status == ("uncertain" if failure == "connection" else "pending")
                for row in rows
            )
            assert all("private" not in (row.last_error or "") for row in rows)
            assert all(session.get(MatchResultRow, row.result_id) is not None for row in rows)
        monkeypatch.setattr(
            telegram, "send_email", lambda destination, message: calls.append(destination)
        )
        telegram.dispatch_notifications(
            fixture.runtime.sessions, fixture.runtime.clock.now() + timedelta(hours=2)
        )
        assert len(calls) == (2 if failure == "connection" else 4)


def test_admin_smtp_settings_are_encrypted_persistent_and_isolated(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet
    from test_accounts import register

    from tender_system.api.api import create_app
    from tender_system.operator import smtp_settings
    from tender_system.persistence.storage import OperatorConfigRow, UserRow, session_factory

    monkeypatch.setenv("QUARRY_SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    sessions = session_factory(migrated_engine)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        smtp_settings,
        "send_email",
        lambda destination, message, config: sent.append((destination, config["password"])),
    )
    payload = {
        "host": "smtp.example.org",
        "port": 587,
        "security": "starttls",
        "sender": "quarry@example.org",
        "username": "quarry",
        "password": "smtp-test-secret",
    }
    app = create_app(sessions, operator_mode=False)
    with TestClient(app) as customer:
        assert customer.get("/api/v1/admin/smtp").status_code == 401
        register(customer, "customer@example.org")
        assert customer.put("/api/v1/admin/smtp", json=payload).status_code == 403
        assert (
            customer.post(
                "/api/v1/admin/smtp/test", json={"destination": "customer@example.org"}
            ).status_code
            == 403
        )
    with TestClient(create_app(sessions, operator_mode=False)) as admin:
        register(admin, "admin@example.org")
        with sessions.begin() as session:
            user = session.scalar(select(UserRow).where(UserRow.email == "admin@example.org"))
            assert user is not None
            user.role = "admin"
        assert admin.put("/api/v1/admin/smtp", json=payload).status_code == 200
        status = admin.get("/api/v1/admin/smtp")
        assert status.status_code == 200 and status.json()["password_configured"]
        assert "smtp-test-secret" not in status.text
        cookie = admin.cookies.get("quarry_session")
    with sessions() as session:
        row = session.get(OperatorConfigRow, "smtp.config")
        assert row is not None and "smtp-test-secret" not in str(row.value)
    with TestClient(create_app(sessions, operator_mode=False)) as restarted:
        assert cookie is not None
        restarted.cookies.set("quarry_session", cookie)
        assert restarted.post(
            "/api/v1/admin/smtp/test", json={"destination": "admin@example.org"}
        ).json() == {"status": "accepted"}
        assert sent == [("admin@example.org", "smtp-test-secret")]
        assert (
            restarted.put(
                "/api/v1/admin/smtp", json={**payload, "host": "https://bad.example"}
            ).status_code
            == 422
        )
