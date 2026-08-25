from __future__ import annotations

import json
import os
import smtplib
import ssl
import threading
import time
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from .forex_report import render_forex_report


UTC = timezone.utc


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _recipients() -> list[str]:
    raw = os.getenv("FOREX_EMAIL_RECIPIENTS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


class ForexReportEmailer:
    """Best-effort, idempotent delivery for the production Forex dashboard.

    Email failures are deliberately isolated from the executor scan. The
    SQLite-backed ledger supplies restart-safe one-send-per-hour state, while
    the in-process lock prevents overlapping SMTP attempts.
    """

    def __init__(self, ledger):
        self.ledger = ledger
        self._lock = threading.Lock()
        self._inflight = False
        self._last_attempt_monotonic = 0.0

    @staticmethod
    def enabled() -> bool:
        return _truthy(os.getenv("FOREX_EMAIL_REPORT_ENABLED"))

    @staticmethod
    def timezone() -> ZoneInfo:
        name = os.getenv("FOREX_EMAIL_TIMEZONE", "America/New_York").strip()
        try:
            return ZoneInfo(name)
        except Exception:
            return ZoneInfo("America/New_York")

    @classmethod
    def hour_key(cls, now: datetime | None = None) -> str:
        current = (now or datetime.now(UTC)).astimezone(cls.timezone())
        return current.strftime("%Y-%m-%dT%H%z")

    @classmethod
    def subject(cls, now: datetime | None = None) -> str:
        current = (now or datetime.now(UTC)).astimezone(cls.timezone())
        return f"[HOURLY] Forex Live Trading Dashboard - {current:%Y-%m-%d %H}:00 ET"

    @staticmethod
    def provider() -> str:
        return os.getenv("FOREX_EMAIL_PROVIDER", "smtp").strip().lower()

    @classmethod
    def _configuration(cls) -> dict:
        config = {
            "provider": cls.provider(),
            "from_address": os.getenv("FOREX_EMAIL_FROM", "").strip(),
            "recipients": _recipients(),
            "timeout": max(5, min(60, int(os.getenv("FOREX_EMAIL_TIMEOUT_SECONDS",
                                                   os.getenv("FOREX_EMAIL_SMTP_TIMEOUT_SECONDS", "20"))))),
        }
        missing = [name for name in ("from_address",) if not config[name]]
        if not config["recipients"]:
            missing.append("recipients")
        if config["provider"] == "resend":
            config["api_key"] = os.getenv("FOREX_EMAIL_RESEND_API_KEY", "").strip()
            if not config["api_key"]:
                missing.append("resend_api_key")
        elif config["provider"] == "smtp":
            config.update({
                "host": os.getenv("FOREX_EMAIL_SMTP_HOST", "smtp.gmail.com").strip(),
                "port": int(os.getenv("FOREX_EMAIL_SMTP_PORT", "587")),
                "username": os.getenv("FOREX_EMAIL_SMTP_USERNAME", "").strip(),
                "password": os.getenv("FOREX_EMAIL_SMTP_PASSWORD", ""),
                "starttls": _truthy(os.getenv("FOREX_EMAIL_SMTP_STARTTLS", "true")),
            })
            missing.extend(name for name in ("host", "username", "password") if not config[name])
        else:
            raise ValueError("FOREX_EMAIL_PROVIDER must be resend or smtp")
        if missing:
            raise ValueError("missing Forex email configuration: " + ", ".join(missing))
        return config

    @staticmethod
    def _content(report: dict, now: datetime | None = None) -> dict:
        return {
            "subject": ForexReportEmailer.subject(now),
            "text": "The production Forex dashboard is included as HTML in this message.",
            "html": render_forex_report(report),
        }

    @classmethod
    def _send_resend(cls, config: dict, content: dict) -> None:
        payload = json.dumps({
            "from": config["from_address"],
            "to": config["recipients"],
            "subject": content["subject"],
            "text": content["text"],
            "html": content["html"],
        }).encode()
        request = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
                "User-Agent": "primus-forex-reporter/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
            if not 200 <= int(getattr(response, "status", 0)) < 300:
                raise OSError(f"Resend returned HTTP {getattr(response, 'status', 'unknown')}")
            response.read()

    @staticmethod
    def _send_smtp(config: dict, content: dict) -> None:
        message = EmailMessage()
        message["Subject"] = content["subject"]
        message["From"] = config["from_address"]
        message["To"] = ", ".join(config["recipients"])
        message.set_content(content["text"])
        message.add_alternative(content["html"], subtype="html")
        context = ssl.create_default_context()
        if config["port"] == 465:
            with smtplib.SMTP_SSL(config["host"], config["port"], timeout=config["timeout"],
                                  context=context) as client:
                client.login(config["username"], config["password"])
                client.send_message(message)
        else:
            with smtplib.SMTP(config["host"], config["port"], timeout=config["timeout"]) as client:
                client.ehlo()
                if config["starttls"]:
                    client.starttls(context=context)
                    client.ehlo()
                client.login(config["username"], config["password"])
                client.send_message(message)

    @classmethod
    def _send(cls, report: dict, now: datetime | None = None) -> None:
        config = cls._configuration()
        message_content = cls._content(report, now)
        if config["provider"] == "resend":
            cls._send_resend(config, message_content)
        else:
            cls._send_smtp(config, message_content)

    def maybe_send(self, report: dict, now: datetime | None = None) -> dict:
        if not self.enabled():
            return {"status": "DISABLED"}
        hour = self.hour_key(now)
        if self.ledger.setting("forex_email_last_sent_hour") == hour:
            return {"status": "DUPLICATE_SUPPRESSED", "hour": hour}
        retry_seconds = max(60, int(os.getenv("FOREX_EMAIL_RETRY_SECONDS", "300")))
        with self._lock:
            elapsed = time.monotonic() - self._last_attempt_monotonic
            if self._inflight or (self._last_attempt_monotonic and elapsed < retry_seconds):
                return {"status": "RETRY_PENDING", "hour": hour}
            self._inflight = True
            self._last_attempt_monotonic = time.monotonic()

        snapshot = dict(report)
        thread = threading.Thread(target=self._deliver, args=(snapshot, hour, now), daemon=True)
        thread.start()
        return {"status": "QUEUED", "hour": hour}

    def _deliver(self, report: dict, hour: str, now: datetime | None) -> None:
        try:
            self._send(report, now)
            self.ledger.set_setting("forex_email_last_sent_hour", hour)
            self.ledger.set_setting("forex_email_last_error", "")
            self.ledger.event("FOREX_EMAIL_SENT", {"hour": hour, "recipient_count": len(_recipients())})
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
            self.ledger.set_setting("forex_email_last_error", error)
            self.ledger.event("FOREX_EMAIL_FAILED", {"hour": hour, "error": error})
        finally:
            with self._lock:
                self._inflight = False

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "provider": self.provider(),
            "last_sent_hour": self.ledger.setting("forex_email_last_sent_hour"),
            "last_error": self.ledger.setting("forex_email_last_error"),
            "inflight": self._inflight,
        }
