"""Outbound email via SMTP — used by companion apps (iOS) to deliver
verification codes.

Configure once in the CLI:
    /set email.smtp_host smtp.gmail.com
    /set email.smtp_port 587
    /set email.username you@gmail.com
    /set email.password <app password>
    /set email.from you@gmail.com        (optional, defaults to username)

For Gmail, create an app password at https://myaccount.google.com/apppasswords.
Port 465 uses implicit SSL; anything else attempts STARTTLS.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

NOT_CONFIGURED = (
    "email is not configured on the gateway — run these in the cagentic CLI:\n"
    "/set email.smtp_host smtp.gmail.com\n"
    "/set email.smtp_port 587\n"
    "/set email.username you@gmail.com\n"
    "/set email.password <app password>"
)


def send_verification(cfg: dict, to: str, code: str, name: str = "") -> str | None:
    """Send a verification-code email. Returns an error string, or None on success."""
    if "@" not in to:
        return f"invalid recipient address: {to!r}"
    if not code:
        return "empty verification code"

    em = cfg.get("email") or {}
    host = em.get("smtp_host")
    if not host:
        return NOT_CONFIGURED
    port = int(em.get("smtp_port", 587))
    username = em.get("username") or ""
    password = em.get("password") or ""
    sender = em.get("from") or username or "cagentic@localhost"

    msg = EmailMessage()
    msg["Subject"] = f"{code} is your Cagentic verification code"
    msg["From"] = sender
    msg["To"] = to
    greeting = f"Hi {name}," if name else "Hi,"
    msg.set_content(
        f"{greeting}\n"
        f"\n"
        f"Your Cagentic verification code is:\n"
        f"\n"
        f"    {code}\n"
        f"\n"
        f"It expires in 10 minutes. If you didn't request this, you can\n"
        f"safely ignore this email.\n"
    )

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
        with server:
            if port != 465:
                server.ehlo()
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass
            if username and password:
                server.login(username, password)
            server.send_message(msg)
        return None
    except Exception as e:  # noqa: BLE001 — surface any SMTP failure to the client
        return f"{type(e).__name__}: {e}"
