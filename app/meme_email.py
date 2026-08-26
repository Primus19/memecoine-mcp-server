from __future__ import annotations

import os
import threading
import json
import time
from datetime import datetime,timezone
from zoneinfo import ZoneInfo

from .forex_email import ForexReportEmailer,_truthy
from .meme_report import render_meme_report


class MemeReportEmailer(ForexReportEmailer):
    @staticmethod
    def enabled()->bool:return _truthy(os.getenv("MEME_EMAIL_REPORT_ENABLED",os.getenv("FOREX_EMAIL_REPORT_ENABLED","false")))

    @staticmethod
    def provider()->str:return os.getenv("MEME_EMAIL_PROVIDER",os.getenv("FOREX_EMAIL_PROVIDER","gmail_api")).strip().lower()

    @staticmethod
    def timezone()->ZoneInfo:
        try:return ZoneInfo(os.getenv("MEME_EMAIL_TIMEZONE",os.getenv("FOREX_EMAIL_TIMEZONE","America/New_York")))
        except Exception:return ZoneInfo("America/New_York")

    @staticmethod
    def recipients()->list[str]:return [x.strip() for x in os.getenv("MEME_EMAIL_RECIPIENTS",os.getenv("FOREX_EMAIL_RECIPIENTS","")).split(",") if x.strip()]

    @classmethod
    def subject(cls,now:datetime|None=None,alert:dict|None=None)->str:
        current=(now or datetime.now(timezone.utc)).astimezone(cls.timezone())
        event=str((alert or {}).get("kind") or "TRADE ALERT").replace("_"," ")
        return f"[TRADE] Meme Coin {event} - {current:%Y-%m-%d %H:%M ET}"

    @classmethod
    def _configuration(cls)->dict:
        recipients=cls.recipients();config={"provider":cls.provider(),"from_address":os.getenv("MEME_EMAIL_FROM",os.getenv("FOREX_EMAIL_FROM","")).strip(),"recipients":recipients,"timeout":max(5,min(60,int(os.getenv("MEME_EMAIL_TIMEOUT_SECONDS",os.getenv("FOREX_EMAIL_TIMEOUT_SECONDS","20"))))) }
        config.update({"client_id":os.getenv("MEME_EMAIL_GMAIL_CLIENT_ID",os.getenv("FOREX_EMAIL_GMAIL_CLIENT_ID","")).strip(),"client_secret":os.getenv("MEME_EMAIL_GMAIL_CLIENT_SECRET",os.getenv("FOREX_EMAIL_GMAIL_CLIENT_SECRET","")).strip(),"refresh_token":os.getenv("MEME_EMAIL_GMAIL_REFRESH_TOKEN",os.getenv("FOREX_EMAIL_GMAIL_REFRESH_TOKEN","")).strip()})
        missing=[k for k in ("from_address","client_id","client_secret","refresh_token") if not config[k]]
        if not recipients:missing.append("recipients")
        if config["provider"]!="gmail_api":raise ValueError("MEME_EMAIL_PROVIDER must be gmail_api")
        if missing:raise ValueError("missing Meme email configuration: "+", ".join(missing))
        return config

    @staticmethod
    def _content(report:dict,now:datetime|None=None)->dict:
        alert=report.get("_meme_alert") or {}
        return {"subject":MemeReportEmailer.subject(now,alert),"text":"A confirmed trade action or critical trading event occurred. The production dashboard is included as HTML.","html":render_meme_report(report)}

    # Routine scans, model reviews, unchanged positions and health polling are
    # deliberately excluded. Critical alerts are restricted to state-changing
    # safety events so transient polling errors cannot create email storms.
    ALERT_KINDS={"ENTRY_FILLED","POSITION_CLOSED","PAUSED","ENTRY_PROTECTION_FAILED",
                 "UNPROTECTED_TRADE_EMERGENCY_CLOSE","EMERGENCY_FLATTEN_SUBMITTED"}

    def maybe_send(self,report:dict,now:datetime|None=None)->dict:
        if not self.enabled():return {"status":"DISABLED"}
        sent=set(json.loads(self.ledger.setting("meme_email_sent_event_ids","[]") or "[]"))
        pending=json.loads(self.ledger.setting("meme_email_pending_alerts","[]") or "[]")
        by_id={str(x.get("event_id")):x for x in pending if x.get("event_id")}
        for event in report.get("notification_events") or []:
            kind=str(event.get("kind") or "")
            event_id=str(event.get("seq") or event.get("digest") or "")
            if kind in self.ALERT_KINDS and event_id and event_id not in sent:
                by_id[event_id]={"event_id":event_id,"kind":kind,"event":event,"report":report}
        pending=[x for key,x in by_id.items() if key not in sent]
        self.ledger.set_setting("meme_email_pending_alerts",json.dumps(pending,default=str))
        if not pending:return {"status":"NO_NEW_TRADE_OR_CRITICAL_EVENT"}
        retry=max(60,int(os.getenv("MEME_EMAIL_RETRY_SECONDS",os.getenv("FOREX_EMAIL_RETRY_SECONDS","300"))))
        with self._lock:
            if self._inflight or (self._last_attempt_monotonic and time.monotonic()-self._last_attempt_monotonic<retry):return {"status":"RETRY_PENDING","pending_alerts":len(pending)}
            self._inflight=True;self._last_attempt_monotonic=time.monotonic()
        threading.Thread(target=self._deliver,args=(pending,now),daemon=True).start();return {"status":"QUEUED","pending_alerts":len(pending)}

    def _deliver(self,alerts:list[dict],now:datetime|None)->None:
        try:
            sent=list(json.loads(self.ledger.setting("meme_email_sent_event_ids","[]") or "[]"))
            for alert in alerts:
                report=dict(alert["report"]);report["_meme_alert"]=alert
                self._send(report,now);sent.append(str(alert["event_id"]))
                self.ledger.set_setting("meme_email_sent_event_ids",json.dumps(sent[-500:]))
                self.ledger.set_setting("meme_email_pending_alerts",json.dumps([x for x in alerts if str(x.get("event_id")) not in set(sent)],default=str))
                self.ledger.set_setting("meme_email_last_error","");self.ledger.event("MEME_TRADE_ALERT_SENT",{"kind":alert["kind"],"recipient_count":len(self.recipients())})
        except Exception as exc:
            error=f"{type(exc).__name__}: {str(exc)[:240]}";self.ledger.set_setting("meme_email_last_error",error);self.ledger.event("MEME_EMAIL_FAILED",{"error":error})
        finally:
            with self._lock:self._inflight=False

    def status(self)->dict:return {"enabled":self.enabled(),"provider":self.provider(),"mode":"TRADE_AND_CRITICAL_EVENTS_ONLY","sent_event_count":len(json.loads(self.ledger.setting("meme_email_sent_event_ids","[]") or "[]")),"pending_alert_count":len(json.loads(self.ledger.setting("meme_email_pending_alerts","[]") or "[]")),"last_error":self.ledger.setting("meme_email_last_error"),"inflight":self._inflight}
