from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.message import EmailMessage


class EmailNotifier:
    """Optional SMTP notifier; absence of configuration never blocks risk controls."""

    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "")
        self.port = int(os.getenv("SMTP_PORT", "465"))
        self.username = os.getenv("SMTP_USERNAME", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.sender = os.getenv("SMTP_FROM", self.username)
        self.recipient = os.getenv("TRADE_REPORT_EMAIL", "")

    @property
    def configured(self) -> bool:
        return all((self.host, self.username, self.password, self.sender, self.recipient))

    def send(self, subject: str, event: dict) -> dict:
        if not self.configured:
            return {"sent": False, "reason": "email_not_configured"}
        rows = "".join(
            f"<tr><td style='padding:6px;border-bottom:1px solid #ddd'>{html.escape(str(k))}</td>"
            f"<td style='padding:6px;border-bottom:1px solid #ddd'>{html.escape(str(v))}</td></tr>"
            for k, v in event.items()
        )
        body = (
            "<div style='font-family:Arial;max-width:720px;margin:auto'>"
            "<h2 style='background:#172554;color:white;padding:16px;border-radius:12px'>Coinbase $25 Pilot</h2>"
            f"<table style='width:100%;border-collapse:collapse'>{rows}</table>"
            "<p style='color:#b45309'>Live trading is speculative. Profit is not guaranteed and the pilot capital can be lost.</p></div>"
        )
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content("Coinbase pilot event: " + ", ".join(f"{k}={v}" for k, v in event.items()))
        message.add_alternative(body, subtype="html")
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=15) as smtp:
            smtp.login(self.username, self.password)
            smtp.send_message(message)
        return {"sent": True, "recipient": self.recipient}
