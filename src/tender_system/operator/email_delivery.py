"""Bounded SMTP transport with certificate validation."""

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any


class EmailConfigurationError(ValueError):
    """SMTP configuration is missing or invalid."""


def send_email(destination: str, message: str, config: dict[str, Any] | None = None) -> None:
    config = config or {}
    host = config.get("host", os.environ.get("QUARRY_SMTP_HOST", ""))
    sender = config.get("sender", os.environ.get("QUARRY_SMTP_FROM", ""))
    security = config.get("security", os.environ.get("QUARRY_SMTP_SECURITY", "starttls"))
    try:
        port = int(
            config.get(
                "port", os.environ.get("QUARRY_SMTP_PORT", "465" if security == "tls" else "587")
            )
        )
        if not host or not sender or security not in {"starttls", "tls"} or not 1 <= port <= 65535:
            raise ValueError
        mail = EmailMessage()
        mail["From"] = sender
        mail["To"] = destination
        mail["Subject"] = "Quarry: relevant tender found"
        mail.set_content(message)
    except ValueError:
        raise EmailConfigurationError("Email transport is not configured correctly") from None
    context = ssl.create_default_context()
    connection: smtplib.SMTP
    if security == "tls":
        connection = smtplib.SMTP_SSL(host, port, timeout=10, context=context)
    else:
        connection = smtplib.SMTP(host, port, timeout=10)
    with connection as smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        username = config.get("username", os.environ.get("QUARRY_SMTP_USERNAME"))
        if username:
            smtp.login(username, config.get("password", os.environ.get("QUARRY_SMTP_PASSWORD", "")))
        smtp.send_message(mail)
