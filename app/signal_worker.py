from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_age_seconds(candidate: dict[str, Any], now: datetime | None = None) -> float:
    stamp = datetime.fromisoformat(str(candidate["source_timestamp"]).replace("Z", "+00:00"))
    return ((now or datetime.now(timezone.utc)) - stamp).total_seconds()


def candidate_digest(candidate: dict[str, Any]) -> str:
    signal_id = str(candidate.get("signal_id", "")).strip()
    if signal_id:
        return hashlib.sha256(("signal-id:" + signal_id).encode()).hexdigest()
    raw = json.dumps(candidate, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def eligible_fresh_candidates(payload: Any, *, max_age_seconds: int = 90) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "candidates" not in payload:
        raise ValueError("signal feed object must contain a candidates list")
    values = payload["candidates"] if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("signal feed must return a list or {'candidates': [...]} object")
    accepted = []
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            age = source_age_seconds(value)
        except Exception:
            continue
        if -5 <= age <= max_age_seconds:
            accepted.append(value)
    return accepted


class SignalWorker:
    """Low-latency bridge from a trusted research feed to the atomic executor.

    The worker never creates scores or fills missing evidence. It only forwards
    fresh complete candidates. The executor remains authoritative for all risk
    gates and live Coinbase execution facts.
    """

    def __init__(self) -> None:
        self.feed_url = os.environ["SIGNAL_FEED_URL"]
        self.executor_url = os.getenv("EXECUTOR_BASE_URL", os.environ["PUBLIC_BASE_URL"]).rstrip("/")
        self.executor_token = os.environ["REST_API_TOKEN"]
        self.feed_token = os.getenv("SIGNAL_FEED_BEARER_TOKEN", "")
        self.interval = min(60, max(5, int(os.getenv("SIGNAL_SCAN_INTERVAL_SECONDS", "15"))))
        self.max_age = min(110, max(15, int(os.getenv("SIGNAL_MAX_AGE_SECONDS", "90"))))
        self.status_path = Path(os.getenv("SIGNAL_WORKER_STATUS_PATH", "/app/data/signal_worker_status.json"))
        self.seen: dict[str, float] = {}

    def request_json(self, url: str, *, token: str = "", payload: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "primus-signal-worker/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode())

    def write_status(self, **values: Any) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        content = {"updated_at": utcnow(), **values}
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(content, sort_keys=True, indent=2))
        temporary.replace(self.status_path)

    def cleanup_seen(self) -> None:
        cutoff = time.time() - 3600
        self.seen = {key: at for key, at in self.seen.items() if at >= cutoff}

    def cycle(self) -> dict[str, Any]:
        self.cleanup_seen()
        health = self.request_json(f"{self.executor_url}/health")
        if health.get("mode") not in {"DRY_RUN_LOCKED", "LIVE_ARMED"}:
            raise RuntimeError("executor health did not return a recognized mode")
        feed = self.request_json(self.feed_url, token=self.feed_token)
        fresh = eligible_fresh_candidates(feed, max_age_seconds=self.max_age)
        forwarded = []
        for candidate in fresh:
            digest = candidate_digest(candidate)
            if digest in self.seen:
                continue
            self.seen[digest] = time.time()
            try:
                result = self.request_json(
                    f"{self.executor_url}/api/auto-candidate",
                    token=self.executor_token,
                    payload=candidate,
                )
                forwarded.append({"digest": digest, "status": "accepted", "result": result})
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:2000]
                forwarded.append({"digest": digest, "status": "rejected", "http_status": exc.code, "detail": detail})
        result = {"mode": health["mode"], "feed_candidates": len(fresh), "forwarded": forwarded}
        self.write_status(ok=True, **result)
        return result

    def run(self) -> None:
        failures = 0
        while True:
            try:
                result = self.cycle()
                failures = 0
                print(json.dumps({"at": utcnow(), "event": "SIGNAL_SCAN", **result}, default=str), flush=True)
            except Exception as exc:
                failures += 1
                self.write_status(ok=False, error=type(exc).__name__, detail=str(exc)[:2000], failures=failures)
                print(json.dumps({"at": utcnow(), "event": "SIGNAL_SCAN_ERROR", "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
            time.sleep(min(60, self.interval * max(1, failures)))


def main() -> None:
    if os.getenv("SIGNAL_WORKER_ENABLED", "false").lower() != "true":
        raise SystemExit("SIGNAL_WORKER_ENABLED is not true; refusing to start")
    SignalWorker().run()


if __name__ == "__main__":
    main()
