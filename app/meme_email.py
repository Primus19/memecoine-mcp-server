from __future__ import annotations

import os
import threading
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
    def subject(cls,now:datetime|None=None)->str:
        current=(now or datetime.now(timezone.utc)).astimezone(cls.timezone())
        return f"[HOURLY] Meme Coin Live Trading Dashboard - {current:%Y-%m-%d %H}:00 ET"

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
    def _content(report:dict,now:datetime|None=None)->dict:return {"subject":MemeReportEmailer.subject(now),"text":"The production Meme Coin dashboard is included as HTML in this message.","html":render_meme_report(report)}

    def maybe_send(self,report:dict,now:datetime|None=None)->dict:
        if not self.enabled():return {"status":"DISABLED"}
        hour=self.hour_key(now)
        if self.ledger.setting("meme_email_last_sent_hour")==hour:return {"status":"DUPLICATE_SUPPRESSED","hour":hour}
        retry=max(60,int(os.getenv("MEME_EMAIL_RETRY_SECONDS",os.getenv("FOREX_EMAIL_RETRY_SECONDS","300"))))
        with self._lock:
            if self._inflight or (self._last_attempt_monotonic and time.monotonic()-self._last_attempt_monotonic<retry):return {"status":"RETRY_PENDING","hour":hour}
            self._inflight=True;self._last_attempt_monotonic=time.monotonic()
        threading.Thread(target=self._deliver,args=(dict(report),hour,now),daemon=True).start();return {"status":"QUEUED","hour":hour}

    def _deliver(self,report:dict,hour:str,now:datetime|None)->None:
        try:
            self._send(report,now);self.ledger.set_setting("meme_email_last_sent_hour",hour);self.ledger.set_setting("meme_email_last_error","");self.ledger.event("MEME_EMAIL_SENT",{"hour":hour,"recipient_count":len(self.recipients())})
        except Exception as exc:
            error=f"{type(exc).__name__}: {str(exc)[:240]}";self.ledger.set_setting("meme_email_last_error",error);self.ledger.event("MEME_EMAIL_FAILED",{"hour":hour,"error":error})
        finally:
            with self._lock:self._inflight=False

    def status(self)->dict:return {"enabled":self.enabled(),"provider":self.provider(),"last_sent_hour":self.ledger.setting("meme_email_last_sent_hour"),"last_error":self.ledger.setting("meme_email_last_error"),"inflight":self._inflight}
