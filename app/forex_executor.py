from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .broker_adapters import BrokerError, OandaAdapter
from .forex_report import render_forex_report
from .multi_asset import AssetPolicy, ForexEngine, MultiAssetRejected

UTC = timezone.utc
LOCK = threading.RLock()
STATE = {"ok": False, "mode": "STARTING", "last_scan": "", "last_error": "", "open_positions": 0,
         "report": {}}


def truthy(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL,
              type TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL,
              record_hash TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS intents(
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              symbol TEXT NOT NULL, side TEXT NOT NULL, entry_price REAL NOT NULL, quantity REAL NOT NULL,
              stop_price REAL NOT NULL, target_price REAL NOT NULL, maximum_loss_usd REAL NOT NULL,
              mode TEXT NOT NULL, status TEXT NOT NULL, broker_order_id TEXT, broker_trade_id TEXT,
              realized_pnl_usd REAL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def event(self, kind: str, payload: dict) -> None:
        with self.lock, self.db:
            row = self.db.execute("SELECT record_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            previous = row[0] if row else "GENESIS"
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            stamp = utcnow()
            digest = hashlib.sha256(f"{previous}|{stamp}|{kind}|{body}".encode()).hexdigest()
            self.db.execute("INSERT INTO events(recorded_at,type,payload,previous_hash,record_hash) VALUES(?,?,?,?,?)",
                            (stamp, kind, body, previous, digest))

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def open_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM intents WHERE status IN ('PAPER_OPEN','SUBMITTED','OPEN')").fetchone()[0])

    def has_intent(self, intent_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM intents WHERE id=?", (intent_id,)).fetchone() is not None

    def symbol_in_cooldown(self, symbol: str, seconds: int) -> bool:
        row = self.db.execute("SELECT created_at FROM intents WHERE symbol=? ORDER BY created_at DESC LIMIT 1", (symbol,)).fetchone()
        if not row: return False
        created = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        return (datetime.now(UTC) - created).total_seconds() < seconds

    def add_intent(self, proposal: dict, mode: str, status: str) -> None:
        with self.db:
            self.db.execute("""INSERT INTO intents(id,created_at,expires_at,symbol,side,entry_price,quantity,stop_price,target_price,
              maximum_loss_usd,mode,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
              (proposal["proposal_id"], utcnow(), proposal["expires_at"], proposal["symbol"], proposal["side"], proposal["reference_price"],
               proposal["quantity"], proposal["stop_price"], proposal["target_price"], proposal["maximum_loss_usd"], mode, status))

    def update_intent(self, intent_id: str, status: str, order_id: str = "", trade_id: str = "") -> None:
        with self.db:
            self.db.execute("UPDATE intents SET status=?,broker_order_id=COALESCE(NULLIF(?,''),broker_order_id),broker_trade_id=COALESCE(NULLIF(?,''),broker_trade_id) WHERE id=?",
                            (status, order_id, trade_id, intent_id))

    def paper_positions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents WHERE status='PAPER_OPEN'").fetchall()]

    def broker_positions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents WHERE status IN ('SUBMITTED','OPEN')").fetchall()]

    def recent_intents(self, limit: int = 25) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    def recent_events(self, limit: int = 25) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT seq,recorded_at,type,record_hash FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()]


def fetch_json(url: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "primus-forex-executor/1.0"}
    token = os.getenv("MULTI_ASSET_FEED_BEARER_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
        return json.loads(response.read().decode())


def live_armed(adapter: OandaAdapter) -> bool:
    return all((
        truthy("FOREX_LIVE_ENABLED"),
        os.getenv("FOREX_LIVE_ACK") == "I_ACCEPT_REAL_MONEY_RISK",
        adapter.environment == "live",
        os.getenv("FOREX_ALLOWED_ACCOUNT_ID", "") == adapter.account,
    ))


def practice_armed(adapter: OandaAdapter) -> bool:
    return all((truthy("FOREX_PRACTICE_EXECUTION_ENABLED"), adapter.environment == "practice",
                os.getenv("FOREX_PRACTICE_ACK") == "I_ACCEPT_PRACTICE_ORDER_EXECUTION"))


def safe_quantity(adapter: OandaAdapter, proposal: dict, risk_usd: float) -> float:
    quote = adapter.price(proposal["symbol"])
    if quote.get("status") != "tradeable":
        raise MultiAssetRejected("broker reports instrument non-tradeable")
    observed = datetime.fromisoformat(str(quote["time"]).replace("Z", "+00:00"))
    age = (datetime.now(UTC) - observed).total_seconds()
    if age < -5 or age > 10:
        raise MultiAssetRejected("broker quote stale")
    factor = float(quote.get("quoteHomeConversionFactors", {}).get("negativeUnits") or 0)
    bids, asks = quote.get("bids", []), quote.get("asks", [])
    mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2 if bids and asks else float(proposal["reference_price"])
    distance = abs(float(proposal["reference_price"]) - float(proposal["stop_price"]))
    if factor <= 0 or distance <= 0:
        raise MultiAssetRejected("risk conversion unavailable")
    units = int(risk_usd / (distance * factor))
    metadata = adapter.instrument(proposal["symbol"])
    margin_rate = float(metadata.get("marginRate") or 1)
    unit_notional_home = mid * factor
    notional_cap = max(1.0, float(os.getenv("FOREX_MAX_NOTIONAL_USD", "50")))
    margin_cap = max(0.50, float(os.getenv("FOREX_MAX_MARGIN_USED_USD", "5")))
    units = min(units, int(notional_cap / unit_notional_home), int(margin_cap / (unit_notional_home * margin_rate)))
    if units < 1:
        raise MultiAssetRejected("risk cap cannot support minimum unit")
    return float(units)


class Executor:
    def __init__(self):
        self.adapter = OandaAdapter()
        path = os.getenv("FOREX_LEDGER_PATH", "/app/data/forex.sqlite3")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.ledger = Ledger(path)
        self.engine = ForexEngine(AssetPolicy.from_env())
        self.max_risk = min(2.50, max(0.10, float(os.getenv("FOREX_MAX_RISK_USD", "2.50"))))

    def reconcile(self) -> dict:
        summary = self.adapter.preflight()
        trades = self.adapter.open_trades()
        pending = self.adapter.pending_orders()
        prior_tx = self.ledger.setting("last_transaction_id")
        transactions = self.adapter.transactions_since(prior_tx).get("transactions", []) if prior_tx else []
        if summary.get("last_transaction_id"):
            self.ledger.set_setting("last_transaction_id", summary["last_transaction_id"])
        self.ledger.event("BROKER_RECONCILIATION", {"summary": summary, "open_trades": trades,
                                                     "pending_orders": pending, "transactions": transactions})
        if len(trades) > int(os.getenv("FOREX_MAX_OPEN_POSITIONS", "1")):
            raise BrokerError("broker position count exceeds limit")
        if live_armed(self.adapter) or practice_armed(self.adapter):
            expected = {str(item.get("broker_trade_id") or "") for item in self.ledger.broker_positions() if item.get("broker_trade_id")}
            actual = {str(item.get("id") or "") for item in trades}
            unexpected = actual - expected
            if unexpected: raise BrokerError("unexpected broker trade detected; entries paused")
            for trade in trades:
                if not trade.get("stopLossOrder") or not trade.get("takeProfitOrder"):
                    trade_id = str(trade.get("id") or "")
                    result = self.adapter.close_trade(trade_id) if trade_id else {}
                    self.ledger.event("UNPROTECTED_TRADE_EMERGENCY_CLOSE", {"trade_id": trade_id, "response": result})
                    raise BrokerError("unprotected broker trade closed; entries paused")
        baseline = float(os.getenv("FOREX_LIVE_BASELINE_USD", "0") or 0)
        loss_limit = min(5.0, max(0.50, float(os.getenv("FOREX_DAILY_LOSS_LIMIT_USD", "2.50"))))
        if live_armed(self.adapter) and baseline <= 0:
            raise BrokerError("FOREX_LIVE_BASELINE_USD must be configured")
        today = datetime.now(UTC).date().isoformat()
        if self.ledger.setting("daily_baseline_date") != today:
            self.ledger.set_setting("daily_baseline_date", today)
            self.ledger.set_setting("daily_baseline_nav", str(summary["nav"]))
            self.ledger.event("DAILY_BASELINE_RESET", {"date": today, "nav": summary["nav"]})
        daily_nav = float(self.ledger.setting("daily_baseline_nav", str(summary["nav"])))
        if (live_armed(self.adapter) or practice_armed(self.adapter)) and daily_nav - float(summary["nav"]) >= loss_limit:
            raise BrokerError("daily NAV loss circuit breaker active")
        if (live_armed(self.adapter) or practice_armed(self.adapter)) and float(summary["margin_used"]) > float(os.getenv("FOREX_MAX_MARGIN_USED_USD", "5")):
            raise BrokerError("margin-used circuit breaker active")
        with LOCK:
            STATE["open_positions"] = len(trades)
        return {"summary": summary, "open_trades": trades, "pending_orders": pending,
                "transactions": transactions}

    def process(self, snapshot: dict) -> dict:
        proposal = vars(self.engine.evaluate(snapshot))
        calendar_ok = snapshot.get("calendar_verified") is True and str(snapshot.get("economic_event_source", "")).startswith("https://")
        mode = "LIVE" if live_armed(self.adapter) else "PRACTICE" if practice_armed(self.adapter) else "PAPER_ONLY"
        if mode in {"LIVE", "PRACTICE"} and not calendar_ok:
            raise MultiAssetRejected("verified economic calendar evidence required for broker execution")
        if self.ledger.open_count() >= int(os.getenv("FOREX_MAX_OPEN_POSITIONS", "1")):
            raise MultiAssetRejected("position limit reached")
        if self.ledger.symbol_in_cooldown(str(proposal["symbol"]), int(os.getenv("FOREX_SYMBOL_COOLDOWN_SECONDS", "3600"))):
            raise MultiAssetRejected("symbol cooldown active")
        proposal["maximum_loss_usd"] = self.max_risk
        proposal["quantity"] = safe_quantity(self.adapter, proposal, self.max_risk)
        intent_id = proposal["proposal_id"]
        if self.ledger.has_intent(intent_id):
            return {"status": "DUPLICATE_SUPPRESSED", "id": intent_id}
        if mode == "PAPER_ONLY":
            self.ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
            self.ledger.event("PAPER_FILL", proposal)
            return {"status": "PAPER_FILL", "id": intent_id}
        preflight = self.adapter.preflight()
        if preflight["balance"] <= 0 or preflight["margin_available"] <= 0:
            raise MultiAssetRejected("live account has no available capital")
        if preflight["open_trade_count"] >= int(os.getenv("FOREX_MAX_OPEN_POSITIONS", "1")):
            raise MultiAssetRejected("broker position limit reached")
        self.ledger.add_intent(proposal, mode, "SUBMITTING")
        response = self.adapter.create_order(proposal, client_order_id=intent_id.replace("-", "")[:32])
        rejected = response.get("orderRejectTransaction")
        if rejected:
            self.ledger.update_intent(intent_id, "REJECTED")
            self.ledger.event("ORDER_REJECTED", {"intent_id": intent_id, "response": response})
            raise BrokerError(str(rejected.get("rejectReason") or "order rejected"))
        created = response.get("orderCreateTransaction") or {}
        filled = response.get("orderFillTransaction") or {}
        order_id = str(created.get("id") or filled.get("orderID") or "")
        trade_id = str((filled.get("tradeOpened") or {}).get("tradeID") or "")
        status = "OPEN" if trade_id else "SUBMITTED"
        self.ledger.update_intent(intent_id, status, order_id, trade_id)
        self.ledger.event("ORDER_ACCEPTED", {"intent_id": intent_id, "order_id": order_id, "trade_id": trade_id, "response": response})
        if trade_id:
            protected = {}
            for _ in range(3):
                protected = next((item for item in self.adapter.open_trades() if str(item.get("id")) == trade_id), {})
                if protected.get("stopLossOrder") and protected.get("takeProfitOrder"): break
                time.sleep(1)
            if not protected.get("stopLossOrder") or not protected.get("takeProfitOrder"):
                close = self.adapter.close_trade(trade_id)
                self.ledger.update_intent(intent_id, "EMERGENCY_CLOSED", order_id, trade_id)
                self.ledger.event("ENTRY_PROTECTION_FAILED", {"intent_id": intent_id, "trade_id": trade_id, "close": close})
                raise BrokerError("entry protection missing; trade closed")
        return {"status": status, "id": intent_id, "order_id": order_id, "trade_id": trade_id}

    def supervise_paper(self, snapshots: list[dict]) -> list[dict]:
        marks = {str(item.get("symbol")): float(item.get("price") or 0) for item in snapshots}
        closes = []
        for position in self.ledger.paper_positions():
            price = marks.get(position["symbol"], 0)
            if price <= 0: continue
            side = position["side"]; stop = float(position["stop_price"]); target = float(position["target_price"])
            reason = "STOP" if (side == "BUY" and price <= stop) or (side == "SELL" and price >= stop) else \
                     "TARGET" if (side == "BUY" and price >= target) or (side == "SELL" and price <= target) else ""
            if reason:
                direction = 1 if side == "BUY" else -1
                pnl = (price - float(position["entry_price"])) * float(position["quantity"]) * direction
                self.ledger.update_intent(position["id"], "PAPER_CLOSED")
                with self.ledger.db:
                    self.ledger.db.execute("UPDATE intents SET realized_pnl_usd=? WHERE id=?", (round(pnl, 8), position["id"]))
                event = {"intent_id": position["id"], "symbol": position["symbol"], "fill_price": price,
                         "reason": reason, "realized_pnl_usd": round(pnl, 8)}
                self.ledger.event("PAPER_CLOSE", event); closes.append(event)
        return closes

    def scan(self) -> None:
        reconciliation = self.reconcile()
        payload = fetch_json(os.environ["MULTI_ASSET_FEED_URL"])
        snapshots = payload.get("snapshots", [])
        closes = self.supervise_paper(snapshots)
        outcomes = []
        for snapshot in snapshots:
            try:
                outcomes.append({"symbol": snapshot.get("symbol"), **self.process(snapshot)})
            except Exception as exc:
                outcomes.append({"symbol": snapshot.get("symbol"), "status": "REJECTED", "reason": str(exc)[:300]})
        self.ledger.event("SCAN", {"outcomes": outcomes, "paper_closes": closes})
        report = {"generated_at": utcnow(), "mode": "LIVE_ARMED" if live_armed(self.adapter) else
                  "PRACTICE_ARMED" if practice_armed(self.adapter) else "PAPER_ONLY",
                  "executor_ready": True, "last_scan": utcnow(), "last_error": "",
                  "broker": reconciliation["summary"],
                  "open_trade_count": len(reconciliation["open_trades"]),
                  "pending_order_count": len(reconciliation["pending_orders"]),
                  "transaction_count_since_prior_scan": len(reconciliation["transactions"]),
                  "snapshots": snapshots, "outcomes": outcomes, "paper_closes": closes,
                  "intents": self.ledger.recent_intents(), "events": self.ledger.recent_events(),
                  "baseline_nav": float(self.ledger.setting("daily_baseline_nav", str(reconciliation["summary"]["nav"])))}
        with LOCK:
            STATE["report"] = report
        print(json.dumps({"event": "FOREX_EXECUTOR_SCAN", "outcomes": outcomes, "paper_closes": closes}), flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/health", "/status", "/report", "/report.json"}:
            self.send_error(404); return
        with LOCK:
            state = dict(STATE)
        if self.path == "/report":
            report = dict(state.get("report") or {})
            report.update(mode=state["mode"], executor_ready=state["ok"], last_scan=state["last_scan"],
                          last_error=state["last_error"])
            body = render_forex_report(report).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/report.json":
            payload = dict(state.get("report") or {})
            payload.update(mode=state["mode"], executor_ready=state["ok"], last_scan=state["last_scan"],
                           last_error=state["last_error"])
            status = 200
        elif self.path == "/health":
            payload = {"ok": True, "service": "forex-executor", "mode": state["mode"],
                       "executor_ready": state["ok"], "last_scan": state["last_scan"],
                       "last_error": state["last_error"], "open_positions": state["open_positions"]}
            status = 200
        else:
            payload = state
            status = 200 if state["ok"] else 503
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): return


def main() -> None:
    if not truthy("FOREX_EXECUTOR_ENABLED"):
        raise SystemExit("FOREX_EXECUTOR_ENABLED is not true")
    executor = Executor()
    with LOCK:
        STATE["mode"] = "LIVE_ARMED" if live_armed(executor.adapter) else "PRACTICE_ARMED" if practice_armed(executor.adapter) else "PAPER_ONLY"
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({"event": "FOREX_EXECUTOR_HTTP_READY", "host": "0.0.0.0", "port": port,
                      "mode": STATE["mode"]}), flush=True)
    interval = max(15, int(os.getenv("FOREX_EXECUTOR_INTERVAL_SECONDS", "30")))
    while True:
        try:
            executor.scan()
            with LOCK: STATE.update(ok=True, last_scan=utcnow(), last_error="")
        except Exception as exc:
            with LOCK: STATE.update(ok=False, last_scan=utcnow(), last_error=str(exc)[:500])
            print(json.dumps({"event": "FOREX_EXECUTOR_ERROR", "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
