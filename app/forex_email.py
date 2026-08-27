from __future__ import annotations

import base64
import html
import json
import os
import smtplib
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from .forex_report import render_forex_report


UTC = timezone.utc
BRYNE_STRATEGY_NAME = "Bryne and Lot-Bill Strategy"


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
    def subject(cls, action: dict, now: datetime | None = None) -> str:
        try:
            current = datetime.fromisoformat(
                str(action.get("execution_time")).replace("Z", "+00:00")
            ).astimezone(cls.timezone())
        except Exception:
            current = (now or datetime.now(UTC)).astimezone(cls.timezone())
        kind = str(action.get("email_action") or "CLOSED").upper()
        pair = str(action.get("pair") or "FOREX").replace("_", "/")
        if action.get("strategy_name") == BRYNE_STRATEGY_NAME:
            return f"[TRADE] {BRYNE_STRATEGY_NAME} | NEW {kind} | {pair} | {current:%Y-%m-%d %H:%M} ET"
        return f"[TRADE] Forex {kind} - {pair} - {current:%Y-%m-%d %H:%M} ET"

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
        if config["provider"] == "gmail_api":
            config.update({
                "client_id": os.getenv("FOREX_EMAIL_GMAIL_CLIENT_ID", "").strip(),
                "client_secret": os.getenv("FOREX_EMAIL_GMAIL_CLIENT_SECRET", "").strip(),
                "refresh_token": os.getenv("FOREX_EMAIL_GMAIL_REFRESH_TOKEN", "").strip(),
            })
            missing.extend(name for name in ("client_id", "client_secret", "refresh_token")
                           if not config[name])
        elif config["provider"] == "resend":
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
            raise ValueError("FOREX_EMAIL_PROVIDER must be gmail_api, resend or smtp")
        if missing:
            raise ValueError("missing Forex email configuration: " + ", ".join(missing))
        return config

    @staticmethod
    def _money(value) -> str:
        if value is None:
            return "Not reported"
        amount = float(value)
        if amount > 0:
            return f"+${amount:,.4f}"
        if amount < 0:
            return f"-${abs(amount):,.4f}"
        return f"${amount:,.4f}"

    @classmethod
    def _bryne_html(cls, action: dict) -> str:
        esc = lambda value: html.escape(str(value if value not in (None, "") else "Not reported"))
        pair = esc(action.get("pair")).replace("_", "/")
        kind = esc(action.get("email_action")).upper()
        closed = kind.endswith("CLOSED")
        pnl = float(action.get("realized_pnl_usd") or 0)
        pnl_color = "#15803d" if pnl > 0 else "#dc2626" if pnl < 0 else "#475569"
        side = esc(action.get("side"))
        reason = esc(action.get("entry_reason") or action.get("trigger"))
        exit_reason = esc(action.get("exit_reason")) if closed else "Position remains open"
        return f"""<!doctype html><html><body style="margin:0;background:#f5f3ff;font-family:Arial,sans-serif;color:#24143d">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px">
<table role="presentation" width="760" cellpadding="0" cellspacing="0" style="max-width:760px;background:#fff;border-radius:18px;overflow:hidden;border:1px solid #ddd6fe">
<tr><td style="padding:26px;background:#581c87;color:#fff"><div style="font-size:12px;letter-spacing:1.5px">BRYNE AND LOT-BILL STRATEGY</div>
<div style="display:inline-block;background:#ef4444;color:#fff;padding:6px 11px;border-radius:999px;font-size:13px;font-weight:900;margin-top:8px">NEW ACTION</div>
<div style="font-size:29px;font-weight:800;margin-top:8px">NEW {kind} {pair}</div>
<div style="margin-top:8px;color:#e9d5ff">{esc(action.get('execution_time_et') or action.get('execution_time'))}</div></td></tr>
<tr><td style="padding:22px"><div style="padding:16px;border:3px solid #f59e0b;background:#fef3c7;border-radius:10px">
<b style="font-size:16px;color:#92400e">REASON FOR ENTRY / ACTION</b><div style="margin-top:7px;line-height:1.6;font-size:15px">{reason}</div></div>
<h3 style="margin:22px 0 8px;color:#581c87">Latest strategy action</h3>
<div style="overflow-x:auto"><table width="100%" cellpadding="8" cellspacing="0" style="border:1px solid #ddd6fe;font-size:12px">
<tr style="background:#581c87;color:#fff"><th>New</th><th>Signal UTC</th><th>Pair</th><th>Side</th><th>Status</th><th>Entry</th><th>Stop</th><th>Target</th><th>Risk</th><th>Reason for entry</th><th>Exit reason</th><th>P&amp;L</th></tr>
<tr style="background:#fff7cc"><td><span style="display:inline-block;background:#ef4444;color:#fff;padding:4px 7px;border-radius:999px;font-weight:900">NEW</span></td>
<td>{esc(action.get('signal_time'))}</td><td><b>{pair}</b></td><td>{side}</td><td><b>{'CLOSED' if closed else 'OPEN'}</b></td>
<td>{esc(action.get('entry_price') or action.get('execution_price'))}</td><td>{esc(action.get('stop_price'))}</td><td>{esc(action.get('target_price'))}</td>
<td>{cls._money(action.get('maximum_loss_usd'))}</td><td>{reason}</td><td>{exit_reason}</td><td style="color:{pnl_color};font-weight:800">{cls._money(action.get('realized_pnl_usd'))}</td></tr></table></div>
<div style="margin-top:18px;padding:14px;background:#f5f3ff;border-left:5px solid #7e22ce;border-radius:8px"><b>What triggered this report</b><div style="margin-top:6px;line-height:1.5">{esc(action.get('trigger'))}</div></div>
<div style="margin-top:18px;padding:12px;background:#faf5ff;color:#6b21a8;border-radius:8px"><b>Mode:</b> PAPER ONLY — no broker funds or live margin were used.</div>
<div style="margin-top:20px;padding-top:14px;border-top:1px solid #ddd6fe;color:#64748b;font-size:12px">Bryne and Lot-Bill Strategy action report. The highlighted NEW row is the action that generated this email.</div>
</td></tr></table></td></tr></table></body></html>"""

    @classmethod
    def _trade_html(cls, action: dict) -> str:
        if action.get("strategy_name") == BRYNE_STRATEGY_NAME:
            return cls._bryne_html(action)
        esc = lambda value: html.escape(str(value if value not in (None, "") else "Not reported"))
        pnl = float(action.get("realized_pnl_usd") or 0)
        pnl_color = "#16a34a" if pnl >= 0 else "#dc2626"
        rows = "".join(
            "<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb'>{esc(p.get('instrument')).replace('_','/')}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb'>{esc(p.get('currentUnits'))}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb'>{esc(p.get('price'))}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb'>{esc((p.get('stopLossOrder') or {}).get('price'))}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb'>{esc((p.get('takeProfitOrder') or {}).get('price'))}</td>"
            "</tr>" for p in (action.get("remaining_positions") or [])
        ) or "<tr><td colspan='5' style='padding:10px;color:#64748b'>No positions remain open.</td></tr>"
        warnings = "".join(
            f"<li style='margin:5px 0'>{esc(item)}</li>"
            for item in (action.get("warnings") or [])
        ) or "<li>None</li>"
        pair = esc(action.get("pair")).replace("_", "/")
        return f"""<!doctype html><html><body style="margin:0;background:#eef2ff;font-family:Arial,sans-serif;color:#172033">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;background:#fff;border-radius:18px;overflow:hidden">
<tr><td style="padding:26px;background:#172554;color:#fff"><div style="font-size:12px;letter-spacing:1.5px">FOREX TRADE ACTION</div>
<div style="display:inline-block;background:#dc2626;color:#fff;padding:6px 11px;border-radius:999px;font-size:13px;font-weight:900;margin-top:8px">NEW ACTION</div>
<div style="font-size:30px;font-weight:700;margin-top:7px">NEW {esc(action.get('email_action'))} {pair}</div>
<div style="margin-top:8px;color:#c7d2fe">{esc(action.get('execution_time_et') or action.get('execution_time'))}</div></td></tr>
<tr><td style="padding:22px"><div style="padding:16px;border-left:5px solid #6366f1;background:#eef2ff;border-radius:8px">
<b>Why this report was triggered</b><div style="margin-top:6px;line-height:1.5">{esc(action.get('trigger'))}</div></div>
<div style="margin-top:12px;padding:16px;border:3px solid #f59e0b;background:#fef3c7;border-radius:8px">
<b style="font-size:16px;color:#92400e">REASON FOR ENTRY / ACTION</b><div style="margin-top:7px;line-height:1.6;font-size:15px">{esc(action.get('entry_reason') or action.get('exit_reason') or action.get('trigger'))}</div></div>
<table role="presentation" width="100%" cellpadding="8" cellspacing="0" style="margin-top:16px">
<tr><td style="background:#f8fafc"><b>What happened</b><br>{esc(action.get('action'))}</td><td style="background:#f8fafc"><b>Quantity</b><br>{esc(action.get('filled_quantity'))}</td><td style="background:#f8fafc"><b>Price</b><br>{esc(action.get('execution_price'))}</td></tr>
<tr><td><b>This trade realized</b><br><span style="color:{pnl_color};font-size:22px;font-weight:700">{cls._money(action.get('realized_pnl_usd'))}</span></td><td><b>Today, including open positions</b><br>{cls._money(action.get('daily_pnl_usd'))}</td><td><b>Open-position P&amp;L</b><br>{cls._money(action.get('resulting_unrealized_pnl_usd'))}</td></tr>
<tr><td><b>Total realized account P&amp;L</b><br>{cls._money(action.get('cumulative_realized_pnl_usd'))}</td><td><b>Trade status</b><br>{'CLOSED' if str(action.get('email_action')).upper().endswith('CLOSED') else 'OPEN — profit/loss not final'}</td><td><b>Result</b><br>{'PROFIT' if pnl>0 else 'LOSS' if pnl<0 else 'NOT REALIZED YET'}</td></tr>
<tr><td style="background:#f8fafc"><b>NAV</b><br>{cls._money(action.get('nav'))}</td><td style="background:#f8fafc"><b>Margin used</b><br>{cls._money(action.get('margin_used'))}</td><td style="background:#f8fafc"><b>Margin available</b><br>{cls._money(action.get('margin_available'))}</td></tr></table>
<h3 style="margin:22px 0 8px">Position impact</h3><div style="line-height:1.55">{esc(action.get('position_impact'))}</div>
<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;border:1px solid #e5e7eb"><tr style="background:#172554;color:#fff"><th style="padding:8px">Pair</th><th>Units</th><th>Entry</th><th>Stop loss</th><th>Take profit</th></tr>{rows}</table>
<h3 style="margin:22px 0 8px">Strategy and market context</h3><table width="100%" cellpadding="8" cellspacing="0" style="background:#f8fafc">
<tr><td><b>Signal trigger</b><br>{esc(action.get('signal_trigger'))}</td><td><b>Calendar</b><br>{esc(action.get('calendar_state'))}</td></tr>
<tr><td><b>Executor</b><br>{esc(action.get('executor_state'))}</td><td><b>Risk limits</b><br>{esc(action.get('risk_summary'))}</td></tr></table>
<h3 style="margin:22px 0 8px">Warnings or blockers</h3><ul style="padding-left:20px">{warnings}</ul>
<div style="margin-top:22px;padding-top:14px;border-top:1px solid #e5e7eb;color:#64748b;font-size:12px">Confirmed broker execution report. Account identifiers and credentials are intentionally omitted.</div>
</td></tr></table></td></tr></table></body></html>"""

    @classmethod
    def _content(cls, action: dict, now: datetime | None = None) -> dict:
        return {
            "subject": cls.subject(action, now),
            "text": (
                f"{action.get('strategy_name') or 'Forex'} {action.get('email_action')} {str(action.get('pair') or '').replace('_', '/')}. "
                f"Trigger: {action.get('trigger')}. Filled quantity: {action.get('filled_quantity')}; "
                f"price: {action.get('execution_price')}; realized P&L: "
                f"{cls._money(action.get('realized_pnl_usd'))}. Resulting unrealized P&L: "
                f"{cls._money(action.get('resulting_unrealized_pnl_usd'))}."
            ),
            "html": cls._trade_html(action),
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
    def _mime_message(config: dict, content: dict) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = content["subject"]
        message["From"] = config["from_address"]
        message["To"] = ", ".join(config["recipients"])
        message.set_content(content["text"])
        message.add_alternative(content["html"], subtype="html")
        return message

    @classmethod
    def _send_gmail_api(cls, config: dict, content: dict) -> None:
        token_body = urllib.parse.urlencode({
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": config["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        token_request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "primus-forex-reporter/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(token_request, timeout=config["timeout"]) as response:
            token_payload = json.loads(response.read().decode())
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise OSError("Google OAuth token response did not contain an access token")

        raw = base64.urlsafe_b64encode(cls._mime_message(config, content).as_bytes()).decode()
        send_request = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=json.dumps({"raw": raw}).encode(),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "primus-forex-reporter/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(send_request, timeout=config["timeout"]) as response:
            if not 200 <= int(getattr(response, "status", 0)) < 300:
                raise OSError(f"Gmail API returned HTTP {getattr(response, 'status', 'unknown')}")
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
    def _send(cls, action: dict, now: datetime | None = None) -> None:
        config = cls._configuration()
        message_content = cls._content(action, now)
        if config["provider"] == "gmail_api":
            cls._send_gmail_api(config, message_content)
        elif config["provider"] == "resend":
            cls._send_resend(config, message_content)
        else:
            cls._send_smtp(config, message_content)

    def maybe_send(self, report: dict, now: datetime | None = None) -> dict:
        if not self.enabled():
            return {"status": "DISABLED"}
        sent = set(json.loads(self.ledger.setting("forex_email_sent_action_ids", "[]") or "[]"))
        current_actions = list(report.get("_trade_actions") or [])
        # Establish a history watermark when a ledger is first created. Broker
        # history is context, not a set of new alerts to replay after deploys.
        if self.ledger.setting("forex_email_event_mode_initialized") != "1":
            sent.update(str(item.get("action_id")) for item in current_actions if item.get("action_id"))
            self.ledger.set_setting("forex_email_sent_action_ids", json.dumps(list(sent)[-500:]))
            self.ledger.set_setting("forex_email_pending_actions", "[]")
            self.ledger.set_setting("forex_email_event_mode_initialized", "1")
            return ({"status": "HISTORY_BASELINE_ESTABLISHED", "historical_actions_suppressed": len(current_actions)}
                    if current_actions else {"status": "NO_NEW_TRADE_ACTION"})
        pending = json.loads(self.ledger.setting("forex_email_pending_actions", "[]") or "[]")
        pending_by_id = {str(item.get("action_id")): item for item in pending if item.get("action_id")}
        for action in current_actions:
            action_id = str(action.get("action_id") or "")
            if action_id and action_id not in sent:
                pending_by_id[action_id] = action
        pending = [item for key, item in pending_by_id.items() if key not in sent]
        self.ledger.set_setting("forex_email_pending_actions", json.dumps(pending))
        if not pending:
            return {"status": "NO_NEW_TRADE_ACTION"}
        retry_seconds = max(60, int(os.getenv("FOREX_EMAIL_RETRY_SECONDS", "300")))
        with self._lock:
            elapsed = time.monotonic() - self._last_attempt_monotonic
            if self._inflight or (self._last_attempt_monotonic and elapsed < retry_seconds):
                return {"status": "RETRY_PENDING", "pending_actions": len(pending)}
            self._inflight = True
            self._last_attempt_monotonic = time.monotonic()
        thread = threading.Thread(target=self._deliver, args=(pending, now), daemon=True)
        thread.start()
        return {"status": "QUEUED", "pending_actions": len(pending)}

    def _deliver(self, actions: list[dict], now: datetime | None) -> None:
        try:
            sent = list(json.loads(self.ledger.setting("forex_email_sent_action_ids", "[]") or "[]"))
            sent_set = set(sent)
            remaining = list(actions)
            for action in actions:
                action_id = str(action["action_id"])
                if action_id not in sent_set:
                    self._send(action, now)
                    sent.append(action_id)
                    sent_set.add(action_id)
                remaining = [item for item in remaining if str(item.get("action_id")) != action_id]
                self.ledger.set_setting("forex_email_sent_action_ids", json.dumps(sent[-500:]))
                self.ledger.set_setting("forex_email_pending_actions", json.dumps(remaining))
                self.ledger.set_setting("forex_email_last_error", "")
                self.ledger.event("FOREX_TRADE_EMAIL_SENT", {
                    "action": action.get("email_action"), "pair": action.get("pair"),
                    "recipient_count": len(_recipients())})
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
            self.ledger.set_setting("forex_email_last_error", error)
            self.ledger.event("FOREX_TRADE_EMAIL_FAILED", {"error": error})
        finally:
            with self._lock:
                self._inflight = False

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "provider": self.provider(),
            "sent_action_count": len(json.loads(self.ledger.setting("forex_email_sent_action_ids", "[]") or "[]")),
            "pending_action_count": len(json.loads(self.ledger.setting("forex_email_pending_actions", "[]") or "[]")),
            "last_error": self.ledger.setting("forex_email_last_error"),
            "inflight": self._inflight,
        }
