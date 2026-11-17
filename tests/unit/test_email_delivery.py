"""Email validation, TLS, secret-safe failures, and required notification contents."""

import smtplib
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tender_system.core.domain import NotificationSettings
from tender_system.operator import email_delivery


@pytest.mark.parametrize("email", ["buyer@example.org", "first.last+quarry@example.co.uk"])
def test_notification_email(email: str) -> None:
    assert NotificationSettings(channel="email", email=email).email == email


@pytest.mark.parametrize("email", [None, "", "bad", "a@localhost", "a@example.org\nBcc: x@y.org"])
def test_reject_invalid_notification_email(email: str | None) -> None:
    with pytest.raises(ValidationError):
        NotificationSettings(channel="email", email=email)


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_email_uses_verified_tls_and_bounded_transport(
    monkeypatch: pytest.MonkeyPatch, security: str
) -> None:
    for key, value in {
        "HOST": "smtp.example.org",
        "FROM": "quarry@example.org",
        "SECURITY": security,
        "USERNAME": "quarry",
        "PASSWORD": "not-a-real-password",
    }.items():
        monkeypatch.setenv("QUARRY_SMTP_" + key, value)
    transport = MagicMock()
    monkeypatch.setattr(smtplib, "SMTP", transport)
    monkeypatch.setattr(smtplib, "SMTP_SSL", transport)
    email_delivery.send_email("buyer@example.org", "Tender: Network\nLink: https://example.org/1")
    assert transport.call_args.kwargs["timeout"] == 10
    smtp = transport.return_value.__enter__.return_value
    if security == "starttls":
        assert smtp.starttls.call_args.kwargs["context"].check_hostname
    else:
        assert transport.call_args.kwargs["context"].check_hostname
    smtp.login.assert_called_once_with("quarry", "not-a-real-password")
    mail = smtp.send_message.call_args.args[0]
    assert mail["To"] == "buyer@example.org"
    assert "Tender: Network" in mail.get_content()


@pytest.mark.parametrize("security", ["none", "plaintext", "starttls"])
def test_invalid_configuration_never_opens_connection(
    monkeypatch: pytest.MonkeyPatch, security: str
) -> None:
    monkeypatch.setenv("QUARRY_SMTP_HOST", "")
    monkeypatch.setenv("QUARRY_SMTP_SECURITY", security)
    transport = MagicMock()
    monkeypatch.setattr(smtplib, "SMTP", transport)
    with pytest.raises(email_delivery.EmailConfigurationError):
        email_delivery.send_email("buyer@example.org", "Tender")
    transport.assert_not_called()
