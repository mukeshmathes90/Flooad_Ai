"""
Email notification service for the Flood Prediction and Early Warning System.

Uses Gmail SMTP with SSL on smtp.gmail.com:465. Never sends emails on startup -
only on genuine risk-state transitions. All emails include plain-text alternative
and do not expose credentials. SMTP operations are mocked during tests.
"""
from __future__ import annotations

import smtplib
import ssl
import time
import re
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple, Dict, Any

from backend import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(s: str | None) -> str:
    if not s:
        return "—"
    if len(s) <= 4:
        return "***"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _auth_hint(exc: Exception) -> str:
    """Return a readable authentication error, adding a fix hint when Google
    reports the well-known ``WebLoginRequired`` security block (534)."""
    text = str(exc)
    if "5.7.9" in text or "WebLoginRequired" in text:
        return ("Gmail rejected the login (534 WebLoginRequired). Fix: sign in to "
                "the Gmail account in a web browser once to clear the security "
                "check, make sure 2-Step Verification is ON, then create a fresh "
                "App Password at https://myaccount.google.com/apppasswords and put "
                "it in config.txt.")
    return text[:200]


class EmailDeliveryError(Exception):
    pass


class EmailService:
    MAX_ATTEMPTS = 3
    RETRY_DELAY_SECONDS = 2.0

    def __init__(self) -> None:
        self.enabled = bool(config.EMAIL_ENABLED)
        self.sender = config.EMAIL_SENDER or ""
        self.password = config.EMAIL_APP_PASSWORD or ""
        self.host = config.EMAIL_SMTP_HOST
        self.port = config.EMAIL_SMTP_PORT
        self.use_ssl = config.EMAIL_USE_SSL
        self.mock = bool(config.EMAIL_MOCK)
        self.recipients = list(getattr(config, "EMAIL_RECIPIENTS", []) or [])

    def get_recipients(self, to_addr=None) -> list[str]:
        """Resolve the recipient list.

        Priority: explicit ``to_addr`` (str or list) > configured
        ``EMAIL_RECIPIENTS`` > the sender address itself.  This means a single
        mailbox keeps working even when no recipient is configured.
        """
        if to_addr:
            if isinstance(to_addr, str):
                addrs = [a.strip() for a in re.split(r"[,;\s]+", to_addr) if a.strip()]
            else:
                addrs = [str(a).strip() for a in to_addr if str(a).strip()]
            addrs = [a for a in addrs if "@" in a]
            if addrs:
                return addrs
        if self.recipients:
            return [a for a in self.recipients if "@" in a]
        return [self.sender] if self.sender else []

    def is_configured(self) -> bool:
        if not self.enabled or self.mock:
            return bool(self.sender and self.password) if self.mock else bool(self.sender and self.password)
        return bool(self.sender and self.password)

    def validate_config(self) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "email disabled by EMAIL_ENABLED"
        if not self.sender:
            return False, "missing EMAIL_SENDER"
        if "@" not in self.sender:
            return False, "invalid EMAIL_SENDER format"
        if not self.password:
            return False, "missing EMAIL_APP_PASSWORD (Gmail App Password)"
        return True, "ok"

    def _build_email(self, to_addrs: list[str], subject: str, html_body: str, text_body: str) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(to_addrs)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        return msg

    def send_email(self, to_addr=None, subject: str = "", html_body: str = "", text_body: str = "",
                   is_test: bool = False, force_send: bool = False,
                   mock_override: Optional[bool] = None) -> Dict[str, Any]:
        """Send an email.

        ``is_test`` normally *blocks* the send (so test-scenario readings can
        never spam the operator) and returns ``status='skipped_test'``.  The one
        exception is ``force_send=True``, which is used only by the explicit
        "TEST ..." buttons / "Send test email" button in the dashboard.  Those
        emails are still marked ``is_test`` in the database and carry a ``[TEST]``
        subject tag, but they are a real SMTP send so the operator can verify
        connectivity and layout.
        """
        use_mock = self.mock if mock_override is None else mock_override
        recipients = self.get_recipients(to_addr)
        if use_mock or (is_test and not force_send):
            return {
                "sent": False,
                "delivered": False,
                "status": "skipped_test" if is_test else "mocked",
                "error": None,
                "recipients": recipients,
                "timestamp": _now_iso(),
            }

        ok, reason = self.validate_config()
        if not ok:
            return {
                "sent": False,
                "delivered": False,
                "status": f"not_configured: {reason}",
                "error": reason,
                "recipients": recipients,
                "timestamp": _now_iso(),
            }
        if not recipients:
            return {
                "sent": False,
                "delivered": False,
                "status": "not_configured: no recipients",
                "error": "no recipient addresses configured",
                "recipients": [],
                "timestamp": _now_iso(),
            }

        try:
            last_exc = None
            for attempt in range(1, self.MAX_ATTEMPTS + 1):
                try:
                    # Try the configured transport first, then fall back to the
                    # other common Gmail port so a blocked port does not stop us.
                    transports = [(self.use_ssl, self.port)]
                    fallback = (not self.use_ssl, 587 if self.use_ssl else 465)
                    if fallback[1] != self.port:
                        transports.append(fallback)
                    for use_ssl, port in transports:
                        try:
                            self._deliver(use_ssl, port, recipients, subject, html_body, text_body)
                            return {
                                "sent": True,
                                "delivered": True,
                                "status": "sent",
                                "error": None,
                                "recipients": recipients,
                                "timestamp": _now_iso(),
                            }
                        except smtplib.SMTPAuthenticationError as exc:
                            # Auth failure is terminal - retrying or switching
                            # ports will not help and could lock the account.
                            return {
                                "sent": False,
                                "delivered": False,
                                "status": "failed: authentication",
                                "error": _auth_hint(exc),
                                "recipients": recipients,
                                "timestamp": _now_iso(),
                            }
                        except Exception as exc:  # noqa: BLE001
                            last_exc = exc
                            continue
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(self.RETRY_DELAY_SECONDS * attempt)
            return {
                "sent": False,
                "delivered": False,
                "status": f"failed: {type(last_exc).__name__}" if last_exc else "failed: unknown",
                "error": str(last_exc)[:200] if last_exc else None,
                "recipients": recipients,
                "timestamp": _now_iso(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "sent": False,
                "delivered": False,
                "status": f"failed: {type(exc).__name__}",
                "error": str(exc)[:200],
                "recipients": recipients,
                "timestamp": _now_iso(),
            }

    def _deliver(self, use_ssl: bool, port: int, to_addrs: list[str],
                 subject: str, html_body: str, text_body: str) -> None:
        msg = self._build_email(to_addrs, subject, html_body, text_body)
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.host, port, context=context, timeout=20) as server:
                self._authenticate(server)
                server.sendmail(self.sender, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(self.host, port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                self._authenticate(server)
                server.sendmail(self.sender, to_addrs, msg.as_string())

    def _authenticate(self, server) -> None:
        """Authenticate with an explicit AUTH PLAIN exchange.

        Using the raw command (instead of ``server.login``) lets us read the
        server's real reply. Google, for example, answers a rejected App
        Password with ``534 5.7.9 WebLoginRequired`` and then closes the
        connection, which ``server.login`` would otherwise surface as a
        misleading ``SMTPServerDisconnected``.
        """
        import base64

        token = base64.b64encode(
            f"\x00{self.sender}\x00{self.password}".encode()
        ).decode()
        code, resp = server.docmd("AUTH", "PLAIN " + token)
        if code in (235, 503):
            return
        if code == 334:
            code, resp = server.docmd(token)
            if code in (235, 503):
                return
        raise smtplib.SMTPAuthenticationError(code, resp)


email_service = EmailService()
