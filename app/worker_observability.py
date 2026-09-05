"""Read-only operational evidence; missing components never imply zero exposure."""
from datetime import datetime, timezone
import json
import os
import socket
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen


class DualStackHTTPServer(ThreadingHTTPServer):
    """Serve public IPv4 checks and Railway private IPv6 clients."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def monitoring_health(runtime, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(str(runtime.get("last_scan", "")).replace("Z", "+00:00"))
        age = (now - last).total_seconds()
    except (ValueError, TypeError):
        age = None
    reasons = []
    if age is None or age < 0 or age > 900:
        reasons.append("monitor cycle missing or stale")
    if runtime.get("last_error"):
        reasons.append("monitor cycle failed")
    if runtime.get("feed_health", {}).get("status") == "DEGRADED":
        reasons.append("discovery feed degraded; new entries blocked")
    return {"ok": not reasons, "monitor_age_seconds": age, "health_reasons": reasons}


def worker_evidence():
    url = os.getenv("MULTI_ASSET_WORKER_URL", "http://multi-asset-paper-worker.railway.internal:8080/report.json")
    headers = {"Accept": "application/json"}
    token = os.getenv("MULTI_ASSET_REPORT_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=5) as response:
            report = json.load(response)
        return {"verification": "OBSERVED", **report, **monitoring_health(report)}
    except Exception as exc:
        return {"verification": "UNVERIFIED", "ok": False,
                "open_positions": None, "realized_pnl_usd": None,
                "error": type(exc).__name__,
                "connection_error_type": type(getattr(exc, "reason", exc)).__name__,
                "connection_errno": getattr(getattr(exc, "reason", exc), "errno", None),
                "http_status": getattr(exc, "code", None)}


def decision_funnel(outcomes):
    reasons = {}
    fills = 0
    for item in outcomes:
        if item.get("status") == "PAPER_FILL":
            fills += 1
        else:
            reason = item.get("reason") or "unspecified"
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"evaluated": len(outcomes), "filled": fills,
            "rejected": len(outcomes) - fills, "rejection_reasons": reasons}
