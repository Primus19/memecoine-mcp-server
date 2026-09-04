from __future__ import annotations

import base64
import html
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo


UTC = timezone.utc
STRATEGY = "MULTI_WEEK_CRYPTO_MOMENTUM_V1"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _setting(name: str, fallback: str = "", default: str = "") -> str:
    """Reuse the deployed Forex mail transport until dedicated values are set."""
    return os.getenv(name, os.getenv(fallback, default) if fallback else default)


class MultiWeekCryptoEmailer:
    """Restart-safe trade alerts and periodic summaries for the paper engine."""

    def __init__(self, state_path: str | Path) -> None:
        self.path = Path(state_path)
        self.lock = threading.Lock()
        self.inflight = False
        self.state = self._load()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def enabled() -> bool:
        return _truthy(_setting("MULTI_WEEK_EMAIL_ENABLED", "FOREX_EMAIL_REPORT_ENABLED", "false"))

    @staticmethod
    def _timezone() -> ZoneInfo:
        try:
            return ZoneInfo(_setting("MULTI_WEEK_EMAIL_TIMEZONE", "FOREX_EMAIL_TIMEZONE", "America/New_York"))
        except Exception:
            return ZoneInfo("America/New_York")

    @staticmethod
    def _config() -> dict:
        prefix = "MULTI_WEEK_EMAIL_"
        recipients = [item.strip() for item in _setting(prefix + "RECIPIENTS", "FOREX_EMAIL_RECIPIENTS").split(",") if item.strip()]
        provider = _setting(prefix + "PROVIDER", "FOREX_EMAIL_PROVIDER", "gmail_api").strip().lower()
        config = {
            "provider": provider,
            "from": _setting(prefix + "FROM", "FOREX_EMAIL_FROM").strip(),
            "recipients": recipients,
            "timeout": max(5, min(60, int(_setting(prefix + "TIMEOUT_SECONDS", "FOREX_EMAIL_TIMEOUT_SECONDS", "20")))),
        }
        if provider == "gmail_api":
            config.update(client_id=_setting(prefix + "GMAIL_CLIENT_ID", "FOREX_EMAIL_GMAIL_CLIENT_ID").strip(),
                          client_secret=_setting(prefix + "GMAIL_CLIENT_SECRET", "FOREX_EMAIL_GMAIL_CLIENT_SECRET").strip(),
                          refresh_token=_setting(prefix + "GMAIL_REFRESH_TOKEN", "FOREX_EMAIL_GMAIL_REFRESH_TOKEN").strip())
        elif provider == "resend":
            config["api_key"] = _setting(prefix + "RESEND_API_KEY", "FOREX_EMAIL_RESEND_API_KEY").strip()
        missing = [key for key in ("from", "recipients") if not config.get(key)]
        if provider == "gmail_api":
            missing += [key for key in ("client_id", "client_secret", "refresh_token") if not config.get(key)]
        elif provider == "resend" and not config.get("api_key"):
            missing.append("api_key")
        elif provider not in {"gmail_api", "resend"}:
            missing.append("supported provider")
        if missing:
            raise ValueError("missing multi-week email configuration: " + ", ".join(missing))
        return config

    @classmethod
    def _content(cls, event: dict | None, report: dict, runtime: dict, summary: bool) -> dict:
        now = datetime.now(UTC).astimezone(cls._timezone())
        crypto = report.get("multi_week_crypto") or {}
        feed = runtime.get("feed_health") or {}
        emerging = runtime.get("emerging_discovery") or {}
        if summary:
            subject = f"[PAPER] Multi-Week Crypto Status | {now:%Y-%m-%d %H:%M} ET"
            heading = "FOUR-HOUR STRATEGY STATUS"
            detail = (f"Universe: {feed.get('universe_count', 0)}; open: {crypto.get('open', 0)}; "
                      f"closed: {crypto.get('closed', 0)}; realized P&L: ${float(crypto.get('realized_pnl_usd') or 0):+.4f}; "
                      f"feed: {feed.get('status', 'UNKNOWN')}; emerging tracked: {emerging.get('candidate_count', 0)}; "
                      f"emerging qualified: {emerging.get('qualified_count', 0)}.")
        else:
            kind = str((event or {}).get("type") or "ACTION").replace("PAPER_", "")
            symbol = str((event or {}).get("symbol") or "CRYPTO")
            subject = f"[PAPER TRADE] Multi-Week Crypto {kind} | {symbol} | {now:%Y-%m-%d %H:%M} ET"
            heading = f"NEW {kind}: {symbol}"
            detail = (f"Strategy: {STRATEGY}; price: {(event or {}).get('fill_price') or (event or {}).get('mark_price')}; "
                      f"quantity: {(event or {}).get('quantity') or (event or {}).get('added_quantity') or (event or {}).get('closed_quantity')}; "
                      f"reason: {(event or {}).get('reason') or (event or {}).get('thesis')}; "
                      f"realized P&L: ${float((event or {}).get('realized_pnl_usd') or 0):+.4f}.")
        positions = crypto.get("open_positions") or []
        rows = "".join(
            "<tr>" + "".join(f"<td style='padding:7px;border-bottom:1px solid #ddd'>{html.escape(str(v))}</td>" for v in (
                (f"{p.get('symbol')} (RESEARCH)" if p.get("research_only") else p.get("symbol")),
                p.get("entry_price"), p.get("current_mark_price"),
                p.get("current_unrealized_pnl_usd"), p.get("mfe_usd"), p.get("mae_usd"),
                round(float(p.get("age_minutes") or 0) / 1440, 2))) + "</tr>"
            for p in positions
        ) or "<tr><td colspan='7' style='padding:10px'>No open multi-week positions.</td></tr>"
        emerging_rows = "".join(
            "<tr>" + "".join(f"<td style='padding:7px;border-bottom:1px solid #ddd'>{html.escape(str(v))}</td>" for v in (
                item.get("chain"), item.get("symbol"), item.get("score"), item.get("confirmation_count"),
                "YES" if item.get("security_verified") else "NO",
                "; ".join(item.get("failures") or [])[:240])) + "</tr>"
            for item in (emerging.get("candidates") or [])[:10]
        ) or "<tr><td colspan='6' style='padding:10px'>No emerging candidates met the discovery floors.</td></tr>"
        body = f"""<!doctype html><html><body style='font-family:Arial;color:#172033;background:#f5f7fb;padding:20px'>
<div style='max-width:820px;margin:auto;background:white;border-radius:12px;overflow:hidden'>
<div style='padding:22px;background:#123a63;color:white'><small>MULTI-WEEK CRYPTO — PAPER ONLY</small><h2>{html.escape(heading)}</h2></div>
<div style='padding:22px'><p>{html.escape(detail)}</p><p><b>Last scan:</b> {html.escape(str(runtime.get('last_scan') or 'not available'))}<br>
<b>Feed detail:</b> {html.escape(str(feed.get('last_error') or 'none'))}</p>
<table width='100%' cellspacing='0'><tr style='background:#e8eef5'><th>Coin</th><th>Entry</th><th>Mark</th><th>Open P&amp;L</th><th>MFE</th><th>MAE</th><th>Days</th></tr>{rows}</table>
<h3>Emerging on-chain research</h3>
<table width='100%' cellspacing='0'><tr style='background:#e8eef5'><th>Chain</th><th>Coin</th><th>Score</th><th>Confirmations</th><th>Safety</th><th>Qualification gaps</th></tr>{emerging_rows}</table>
<p style='color:#64748b;font-size:12px'>Research portfolio only. No live cryptocurrency purchase is authorized by this report.</p></div></div></body></html>"""
        return {"subject": subject, "text": detail, "html": body}

    @staticmethod
    def _mime(config: dict, content: dict) -> EmailMessage:
        message = EmailMessage()
        message["Subject"], message["From"] = content["subject"], config["from"]
        message["To"] = ", ".join(config["recipients"])
        message.set_content(content["text"]); message.add_alternative(content["html"], subtype="html")
        return message

    @classmethod
    def _send(cls, content: dict) -> dict:
        config = cls._config()
        if config["provider"] == "resend":
            request = urllib.request.Request("https://api.resend.com/emails", method="POST",
                data=json.dumps({"from": config["from"], "to": config["recipients"], **content}).encode(),
                headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"})
        else:
            token_request = urllib.request.Request("https://oauth2.googleapis.com/token", method="POST",
                data=urllib.parse.urlencode({"client_id": config["client_id"], "client_secret": config["client_secret"],
                    "refresh_token": config["refresh_token"], "grant_type": "refresh_token"}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(token_request, timeout=config["timeout"]) as response:
                token = json.loads(response.read().decode()).get("access_token")
            if not token:
                raise OSError("Google OAuth did not return an access token")
            raw = base64.urlsafe_b64encode(cls._mime(config, content).as_bytes()).decode()
            request = urllib.request.Request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", method="POST",
                data=json.dumps({"raw": raw}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
            if not 200 <= int(getattr(response, "status", 0)) < 300:
                raise OSError(f"email provider returned HTTP {getattr(response, 'status', 'unknown')}")
            payload = json.loads(response.read().decode() or "{}")
        return {"id": payload.get("id"), "subject": content["subject"]}

    def maybe_send(self, events: list[dict], report: dict, runtime: dict) -> dict:
        if not self.enabled():
            return {"enabled": False, "status": "DISABLED", "pending": 0}
        sent = set(self.state.get("sent_event_ids") or [])
        actions = [item for item in events if item.get("strategy") == STRATEGY and item.get("event_id") not in sent]
        interval = max(3600, int(os.getenv("MULTI_WEEK_SUMMARY_INTERVAL_SECONDS", "14400")))
        summary_due = time.time() - float(self.state.get("last_summary_epoch") or 0) >= interval
        if not actions and not summary_due:
            return self.status("NO_NEW_ACTION")
        with self.lock:
            if self.inflight:
                return self.status("DELIVERY_IN_PROGRESS")
            self.inflight = True
        def deliver() -> None:
            try:
                for action in actions:
                    result = self._send(self._content(action, report, runtime, False))
                    sent.add(str(action["event_id"])); self.state["last_subject"] = result["subject"]
                if summary_due:
                    result = self._send(self._content(None, report, runtime, True))
                    self.state["last_summary_epoch"] = time.time(); self.state["last_subject"] = result["subject"]
                self.state.update(sent_event_ids=list(sent)[-1000:], last_error="", last_sent_at=datetime.now(UTC).isoformat())
                self._save()
            except Exception as exc:
                self.state["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"; self._save()
            finally:
                with self.lock: self.inflight = False
        threading.Thread(target=deliver, daemon=True).start()
        return self.status("QUEUED")

    def status(self, status: str = "READY") -> dict:
        return {"enabled": self.enabled(), "status": status,
                "provider": _setting("MULTI_WEEK_EMAIL_PROVIDER", "FOREX_EMAIL_PROVIDER", "gmail_api"),
                "sent_event_count": len(self.state.get("sent_event_ids") or []), "last_sent_at": self.state.get("last_sent_at", ""),
                "last_subject": self.state.get("last_subject", ""), "last_error": self.state.get("last_error", ""),
                "inflight": self.inflight, "pending": 0}
