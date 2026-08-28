from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .broker_adapters import BrokerError, OandaAdapter
from .forex_email import ForexReportEmailer
from .forex_report import render_forex_report
from .multi_asset import AssetPolicy, ForexEngine, MultiAssetRejected
from .validation import promotion_gate
from .version import FOREX_MODEL_VERSION, deployment_info

UTC = timezone.utc
LOCK = threading.RLock()
STATE = {"ok": False, "mode": "STARTING", "last_scan": "", "last_error": "", "open_positions": 0,
         "report": {}}


def truthy(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def five_streak_enabled() -> bool:
    return os.getenv("FOREX_FIVE_STREAK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def broker_client_id(intent_id: str) -> str:
    """Return the exact restart-safe ID written into OANDA extensions."""
    return str(intent_id).replace("-", "")[:32]


def validated_snapshots(payload: dict, now: datetime | None = None) -> list[dict]:
    snapshots = payload.get("snapshots", [])
    if not isinstance(snapshots, list) or not snapshots:
        raise MultiAssetRejected("market feed returned no tradable snapshots")
    try:
        scanned_at = datetime.fromisoformat(str(payload.get("scanned_at", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MultiAssetRejected("market feed scan timestamp is missing or invalid") from exc
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=UTC)
    max_age = max(30, int(os.getenv("FOREX_MARKET_FEED_MAX_AGE_SECONDS", "180")))
    if ((now or datetime.now(UTC)) - scanned_at.astimezone(UTC)).total_seconds() > max_age:
        raise MultiAssetRejected("market feed snapshot is stale")
    if any(item.get("calendar_verified") is not True for item in snapshots):
        raise MultiAssetRejected("market feed lacks verified economic calendar evidence")
    return snapshots


FIVE_STREAK_STRATEGY = "FOREX_FIVE_STREAK_EXPERIMENT"
FIVE_STREAK_FILTERED_STRATEGY = "FOREX_FIVE_STREAK_FILTERED_V3"
FIVE_STREAK_DISPLAY_NAME = "Bryne and Lot-Bill Strategy"


def five_streak_position_pnl(position: dict, price: float) -> float:
    direction = 1 if position["side"] == "BUY" else -1
    risk_distance = abs(float(position["entry_price"]) - float(position["stop_price"]))
    if risk_distance <= 0 or price <= 0:
        return 0.0
    return ((price - float(position["entry_price"])) * direction / risk_distance
            * float(position["maximum_loss_usd"]))


def five_streak_baseline_signals(snapshot: dict) -> list[dict]:
    """Reproduce the attached v2 specification for comparison only."""
    candles = list(snapshot.get("five_streak_candles") or [])
    streak, signals = 0, []
    for index, candle in enumerate(candles):
        if index == 0:
            continue
        opened, closed, prior = float(candle["open"]), float(candle["close"]), float(candles[index - 1]["close"])
        green = closed > opened and closed > prior
        red = closed < opened and closed < prior
        streak = (streak + 1 if streak > 0 else 1) if green else \
                 (streak - 1 if streak < 0 else -1) if red else 0
        # Only the newest closed candle is actionable. Older candles establish
        # streak context but must not create retrospective paper fills.
        if abs(streak) < 5 or index < 4 or index != len(candles) - 1:
            continue
        side = "BUY" if streak > 0 else "SELL"
        anchor = candles[index - 4]
        signal_close = closed
        execution_entry = float(snapshot.get("ask") if side == "BUY" else snapshot.get("bid") or signal_close)
        stop = float(anchor["low"] if side == "BUY" else anchor["high"])
        if (side == "BUY" and stop >= execution_entry) or (side == "SELL" and stop <= execution_entry):
            raise MultiAssetRejected("Five-Streak stop is on the wrong side of executable entry")
        distance = abs(execution_entry - stop)
        target = execution_entry + distance if side == "BUY" else execution_entry - distance
        signal_key = f"{FIVE_STREAK_STRATEGY}:{snapshot['symbol']}:{candle.get('time')}:{side}"
        signals.append({"proposal_id": hashlib.sha256(signal_key.encode()).hexdigest(),
                        "expires_at": utcnow(), "symbol": snapshot["symbol"], "side": side,
                        "reference_price": execution_entry, "signal_close": signal_close,
                        "quantity": 1.0, "stop_price": stop, "target_price": target,
                        "maximum_loss_usd": 0.0, "score": abs(streak),
                        "strategy": FIVE_STREAK_STRATEGY, "signal_time": candle.get("time"),
                        "model_version": "five-streak-v2-paper"})
    return signals


def five_streak_signals(snapshot: dict) -> list[dict]:
    """Filtered v3 challenger: first streak only, liquid and trend-confirmed."""
    allowed = {value.strip() for value in os.getenv(
        "FOREX_FIVE_STREAK_V3_INSTRUMENTS", "USD_JPY,XAU_USD,XAG_USD").split(",") if value.strip()}
    if snapshot.get("symbol") not in allowed:
        return []
    if snapshot.get("session_liquid") is not True:
        raise MultiAssetRejected("Five-Streak v3 requires a liquid session")
    agreement = float(snapshot.get("horizon_agreement") or 0)
    trend = float(snapshot.get("trend_strength") or 0)
    one = float(snapshot.get("change_1h_pct") or 0)
    if agreement < .75 or abs(trend) < .05 or not .02 <= abs(one) <= .50:
        raise MultiAssetRejected("Five-Streak v3 trend, agreement, or 1h anti-chop/anti-chase gate failed")
    candles = list(snapshot.get("five_streak_candles") or [])
    baseline = five_streak_baseline_signals(snapshot)
    if not baseline:
        return []
    proposal = baseline[-1]
    # Extended streak entries created serially correlated losses in the first
    # sample. V3 acts only on the first candle that completes a five-run.
    streak = 0
    for index, candle in enumerate(candles):
        if index == 0: continue
        opened, closed, prior = float(candle["open"]), float(candle["close"]), float(candles[index - 1]["close"])
        green, red = closed > opened and closed > prior, closed < opened and closed < prior
        streak = (streak + 1 if streak > 0 else 1) if green else (streak - 1 if streak < 0 else -1) if red else 0
    if abs(streak) != 5:
        return []
    side = proposal["side"]
    if (side == "BUY") != (trend > 0) or (side == "BUY") != (one > 0):
        raise MultiAssetRejected("Five-Streak v3 direction contradicts trend or 1h move")
    spread_bps = (float(snapshot.get("ask") or 0) - float(snapshot.get("bid") or 0)) / max(float(snapshot.get("price") or proposal["reference_price"]), 1e-12) * 10_000
    if spread_bps > float(os.getenv("FOREX_FIVE_STREAK_V3_MAX_SPREAD_BPS", "3")):
        raise MultiAssetRejected("Five-Streak v3 executable spread too wide")
    risk = abs(proposal["reference_price"] - proposal["stop_price"])
    proposal.update({
        "proposal_id": hashlib.sha256(proposal["proposal_id"].encode()).hexdigest(),
        "target_price": proposal["reference_price"] + (1.5 * risk if side == "BUY" else -1.5 * risk),
        "strategy": FIVE_STREAK_FILTERED_STRATEGY,
        "model_version": "five-streak-filtered-v3-paper",
        "entry_reason": (f"Filtered V3: first five-candle {side} streak; agreement={agreement:.2f}, "
                         f"trend={trend:.4f}, 1h={one:.4f}%, spread={spread_bps:.2f}bps; target=1.5R."),
    })
    return [proposal]


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
              realized_pnl_usd REAL, score REAL, model_version TEXT, closed_at TEXT,
              max_favorable_pnl_usd REAL DEFAULT 0, max_adverse_pnl_usd REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            columns = {str(row[1]) for row in self.db.execute("PRAGMA table_info(intents)")}
            for name, kind in (("score", "REAL"), ("model_version", "TEXT"), ("closed_at", "TEXT"),
                               ("strategy", "TEXT"), ("signal_time", "TEXT"), ("close_reason", "TEXT"),
                               ("close_price", "REAL"),
                               ("entry_reason", "TEXT"),
                               ("max_favorable_pnl_usd", "REAL DEFAULT 0"),
                               ("max_adverse_pnl_usd", "REAL DEFAULT 0")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE intents ADD COLUMN {name} {kind}")

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
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def open_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM intents WHERE status IN ('PAPER_OPEN','SUBMITTING','SUBMITTED','OPEN') AND COALESCE(strategy,'') NOT IN (?,?)", (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY)).fetchone()[0])

    def open_risk(self) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(maximum_loss_usd),0) FROM intents WHERE status IN ('PAPER_OPEN','SUBMITTING','SUBMITTED','OPEN') AND COALESCE(strategy,'') NOT IN (?,?)", (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY)).fetchone()
        return float(row[0] or 0)

    def open_symbols(self) -> list[str]:
        return [str(row[0]) for row in self.db.execute("SELECT symbol FROM intents WHERE status IN ('PAPER_OPEN','SUBMITTING','SUBMITTED','OPEN') AND COALESCE(strategy,'') NOT IN (?,?)", (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY)).fetchall()]

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
              maximum_loss_usd,mode,status,score,model_version,strategy,signal_time,entry_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (proposal["proposal_id"], utcnow(), proposal["expires_at"], proposal["symbol"], proposal["side"], proposal["reference_price"],
               proposal["quantity"], proposal["stop_price"], proposal["target_price"], proposal["maximum_loss_usd"], mode, status,
               proposal.get("score"), proposal.get("model_version", FOREX_MODEL_VERSION),
               proposal.get("strategy", "FOREX_CONTROL"), proposal.get("signal_time"),
               proposal.get("entry_reason")))

    def strategy_stats(self, strategy: str) -> dict:
        rows = [dict(row) for row in self.db.execute(
            "SELECT status,realized_pnl_usd FROM intents WHERE strategy=?", (strategy,)).fetchall()]
        closed = [float(row["realized_pnl_usd"]) for row in rows if row["realized_pnl_usd"] is not None]
        wins = sum(value > 0 for value in closed)
        return {"strategy": strategy, "opened": len(rows), "open": sum(row["status"] == "PAPER_OPEN" for row in rows),
                "closed": len(closed), "wins": wins, "losses": len(closed) - wins,
                "win_rate": wins / len(closed) if closed else None, "net_pnl_usd": round(sum(closed), 8),
                "expectancy_usd": sum(closed) / len(closed) if closed else None}

    def strategy_intents(self, strategy: str, limit: int = 50) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT id,created_at,signal_time,closed_at,symbol,side,status,entry_price,stop_price,target_price,"
            "maximum_loss_usd,realized_pnl_usd,close_reason,close_price,entry_reason,max_favorable_pnl_usd,max_adverse_pnl_usd FROM intents WHERE strategy=? "
            "ORDER BY created_at DESC LIMIT ?", (strategy, limit)).fetchall()]

    def update_intent(self, intent_id: str, status: str, order_id: str = "", trade_id: str = "") -> None:
        with self.db:
            self.db.execute("UPDATE intents SET status=?,broker_order_id=COALESCE(NULLIF(?,''),broker_order_id),broker_trade_id=COALESCE(NULLIF(?,''),broker_trade_id) WHERE id=?",
                            (status, order_id, trade_id, intent_id))

    def paper_positions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents WHERE status='PAPER_OPEN'").fetchall()]

    def broker_positions(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents WHERE status IN ('SUBMITTING','SUBMITTED','OPEN')").fetchall()]

    def intent_for_broker_client_id(self, client_id: str) -> dict | None:
        for row in self.broker_positions():
            if broker_client_id(str(row["id"])) == str(client_id):
                return row
        return None

    def recent_intents(self, limit: int = 25) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    def recent_events(self, limit: int = 25) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT seq,recorded_at,type,record_hash FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()]

    def close_broker_intent(self, trade_id: str, realized_pnl_usd: float | None, close_reason: str = "BROKER_CLOSE") -> bool:
        with self.db:
            cursor = self.db.execute("""UPDATE intents SET status='BROKER_CLOSED', realized_pnl_usd=?, closed_at=?,close_reason=?
                              WHERE broker_trade_id=? AND status='OPEN'""",
                            (realized_pnl_usd, utcnow(), close_reason, trade_id))
        return cursor.rowcount == 1

    def import_closed_broker_intent(self, outcome: dict) -> bool:
        """Backfill one tagged OANDA trade that closed before this ledger saw it."""
        with self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO intents(
                id,created_at,expires_at,symbol,side,entry_price,quantity,stop_price,target_price,
                maximum_loss_usd,mode,status,broker_order_id,broker_trade_id,realized_pnl_usd,
                score,model_version,closed_at,strategy,close_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (outcome["proposal_id"], outcome["created_at"], outcome["created_at"],
                 outcome["symbol"], outcome["side"], outcome["entry_price"], outcome["quantity"],
                 outcome["stop_price"], outcome["target_price"], outcome["maximum_loss_usd"],
                 outcome["mode"], "BROKER_CLOSED", outcome["broker_order_id"],
                 outcome["broker_trade_id"], outcome["realized_pnl_usd"], None,
                 FOREX_MODEL_VERSION, outcome["closed_at"], "FOREX_CONTROL", "BROKER_HISTORY"))
        return cursor.rowcount == 1

    def realized_pnl(self) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) FROM intents WHERE realized_pnl_usd IS NOT NULL").fetchone()
        return float(row[0])

    def update_excursions(self, trades: list[dict]) -> None:
        with self.db:
            for trade in trades:
                trade_id = str(trade.get("id") or "")
                if not trade_id:
                    continue
                current = float(trade.get("unrealizedPL") or 0) + float(trade.get("financing") or 0)
                self.db.execute("""UPDATE intents SET
                    max_favorable_pnl_usd=MAX(COALESCE(max_favorable_pnl_usd,0),?),
                    max_adverse_pnl_usd=MIN(COALESCE(max_adverse_pnl_usd,0),?)
                    WHERE broker_trade_id=? AND status='OPEN'""", (current, current, trade_id))

    def model_review(self, minimum_score: float) -> dict:
        rows = [dict(row) for row in self.db.execute("""SELECT score,realized_pnl_usd,symbol,side,closed_at,
                    max_favorable_pnl_usd,max_adverse_pnl_usd
                    FROM intents WHERE status IN ('BROKER_CLOSED','PAPER_CLOSED') AND COALESCE(strategy,'') NOT IN (?,?)
                    AND realized_pnl_usd IS NOT NULL ORDER BY closed_at""", (FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY)).fetchall()]
        pnls = [float(row["realized_pnl_usd"]) for row in rows]
        wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
        sample = len(pnls)
        favorable = [float(row.get("max_favorable_pnl_usd") or 0) for row in rows]
        adverse = [float(row.get("max_adverse_pnl_usd") or 0) for row in rows]
        captured = [float(row["realized_pnl_usd"]) / float(row["max_favorable_pnl_usd"])
                    for row in rows if float(row.get("max_favorable_pnl_usd") or 0) > 0]
        by_symbol = {}
        for row in rows:
            bucket = by_symbol.setdefault(str(row["symbol"]), {"sample_size": 0, "net_pnl_usd": 0.0, "wins": 0})
            bucket["sample_size"] += 1
            bucket["net_pnl_usd"] = round(bucket["net_pnl_usd"] + float(row["realized_pnl_usd"]), 8)
            bucket["wins"] += int(float(row["realized_pnl_usd"]) > 0)
        gate = promotion_gate(pnls, minimum_samples=100, cost_stress=.05)
        review = {
            "model_version": FOREX_MODEL_VERSION, "sample_size": sample,
            "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / sample if sample else None,
            "net_pnl_usd": round(sum(pnls), 8),
            "net_expectancy_usd": sum(pnls) / sample if sample else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "average_win_usd": sum(wins) / len(wins) if wins else None,
            "average_loss_usd": sum(losses) / len(losses) if losses else None,
            "average_max_favorable_excursion_usd": sum(favorable) / sample if sample else None,
            "average_max_adverse_excursion_usd": sum(adverse) / sample if sample else None,
            "average_profit_capture": sum(captured) / len(captured) if captured else None,
            "by_symbol": by_symbol,
            "champion_minimum_score": minimum_score,
            "status": "ELIGIBLE FOR PROSPECTIVE CHALLENGER REVIEW" if gate.eligible else "MODEL LOCKED - COLLECTING EVIDENCE",
            "parameters_changed": False,
            "promotion_gate": {"eligible": gate.eligible, "reasons": list(gate.reasons),
                               "lower_confidence_bound": gate.lower_confidence_bound,
                               "cost_stressed_expectancy": gate.stressed_mean_return},
            "promotion_rule": "At least 100 closed trades, a positive 95% lower confidence bound, positive cost-stressed expectancy, and prospective challenger performance without worse drawdown",
        }
        last_sample = int(self.setting("model_review_sample_size", "-1"))
        if sample != last_sample:
            self.set_setting("model_review_sample_size", str(sample))
            self.event("MODEL_REVIEW", review)
        return review


def closed_trade_details(transactions: list[dict]) -> dict[str, dict]:
    """Return OANDA trade IDs closed/reduced by recent fill transactions."""
    values: dict[str, dict] = {}
    for transaction in transactions:
        if str(transaction.get("type", "")).upper() != "ORDER_FILL":
            continue
        legs = list(transaction.get("tradesClosed") or [])
        if transaction.get("tradeReduced"):
            legs.append(transaction["tradeReduced"])
        for leg in legs:
            trade_id = str(leg.get("tradeID") or "")
            if not trade_id:
                continue
            net = (float(leg.get("realizedPL") or 0) + float(leg.get("financing") or 0)
                   + float(leg.get("dividendAdjustment") or 0)
                   - abs(float(leg.get("guaranteedExecutionFee") or 0)))
            detail = values.get(trade_id, {"pnl": 0.0, "reason": ""})
            detail["pnl"] += net
            detail["reason"] = str(transaction.get("reason") or transaction.get("type") or "BROKER_CLOSE")
            values[trade_id] = detail
    return values


def closed_trade_pnl(transactions: list[dict]) -> dict[str, float]:
    return {trade_id: float(detail["pnl"]) for trade_id, detail in closed_trade_details(transactions).items()}


def historical_managed_trade_outcomes(transactions: list[dict], mode: str) -> list[dict]:
    """Recover closed trades created by this service from OANDA history.

    Only orders carrying our client-extension tag are eligible. This excludes
    manual or unrelated account activity while repairing model/report samples
    after restarts or earlier ledger gaps.
    """
    orders = {str(item.get("id") or ""): item for item in transactions
              if str(item.get("type") or "").upper() == "MARKET_ORDER"
              and (item.get("clientExtensions") or {}).get("tag") == "primus-forex-v1"
              and (item.get("clientExtensions") or {}).get("id")}
    opened: dict[str, dict] = {}
    closed_at: dict[str, str] = {}
    for item in transactions:
        if str(item.get("type") or "").upper() != "ORDER_FILL":
            continue
        order = orders.get(str(item.get("orderID") or ""))
        leg = item.get("tradeOpened") or {}
        trade_id = str(leg.get("tradeID") or "")
        if order and trade_id:
            units = float(leg.get("units") or item.get("units") or order.get("units") or 0)
            entry = float(item.get("price") or order.get("priceBound") or 0)
            stop = float((order.get("stopLossOnFill") or {}).get("price") or 0)
            target = float((order.get("takeProfitOnFill") or {}).get("price") or 0)
            if units and entry > 0 and stop > 0 and target > 0:
                opened[trade_id] = {
                    "proposal_id": str((order.get("clientExtensions") or {})["id"]),
                    "created_at": str(item.get("time") or order.get("time") or utcnow()),
                    "symbol": str(item.get("instrument") or order.get("instrument") or ""),
                    "side": "BUY" if units > 0 else "SELL", "entry_price": entry,
                    "quantity": abs(units), "stop_price": stop, "target_price": target,
                    "maximum_loss_usd": abs(entry - stop) * abs(units),
                    "mode": mode, "broker_order_id": str(item.get("orderID") or ""),
                    "broker_trade_id": trade_id,
                }
        for closed_leg in list(item.get("tradesClosed") or []) + ([item["tradeReduced"]] if item.get("tradeReduced") else []):
            closed_at[str(closed_leg.get("tradeID") or "")] = str(item.get("time") or utcnow())
    pnl = closed_trade_pnl(transactions)
    return [{**value, "realized_pnl_usd": pnl[trade_id],
             "closed_at": closed_at.get(trade_id, utcnow())}
            for trade_id, value in opened.items() if trade_id in pnl]


def transaction_managed_intent_id(trade_id: str, transactions: list[dict]) -> str:
    fills = [item for item in transactions if str(item.get("type", "")).upper() == "ORDER_FILL"
             and str((item.get("tradeOpened") or {}).get("tradeID") or "") == trade_id]
    for fill in fills:
        order_id = str(fill.get("orderID") or "")
        created = next((item for item in transactions
                        if str(item.get("id") or "") == order_id
                        and str(item.get("type", "")).upper() == "MARKET_ORDER"), {})
        extensions = created.get("clientExtensions") or {}
        if extensions.get("tag") == "primus-forex-v1" and extensions.get("id"):
            return str(extensions["id"])
    return ""


def recoverable_managed_trade(trade: dict, maximum_loss_usd: float, transactions: list[dict] | None = None) -> dict | None:
    """Rebuild a ledger intent only for our tagged, broker-protected trade."""
    extensions = trade.get("clientExtensions") or {}
    intent_id = str(extensions.get("id") or "") if extensions.get("tag") == "primus-forex-v1" else ""
    if not intent_id and transactions:
        intent_id = transaction_managed_intent_id(str(trade.get("id") or ""), transactions)
    if not intent_id:
        return None
    stop_order, target_order = trade.get("stopLossOrder") or {}, trade.get("takeProfitOrder") or {}
    if not stop_order.get("price") or not target_order.get("price"):
        return None
    units = float(trade.get("currentUnits") or 0)
    entry = float(trade.get("price") or 0)
    if not units or entry <= 0:
        return None
    return {
        "proposal_id": intent_id,
        "expires_at": str(trade.get("openTime") or utcnow()),
        "symbol": str(trade.get("instrument") or ""),
        "side": "BUY" if units > 0 else "SELL",
        "reference_price": entry,
        "quantity": abs(units),
        "stop_price": float(stop_order["price"]),
        "target_price": float(target_order["price"]),
        "maximum_loss_usd": maximum_loss_usd,
        "score": None,
        "model_version": FOREX_MODEL_VERSION,
    }


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


def safe_quantity(adapter: OandaAdapter, proposal: dict, risk_usd: float,
                  margin_budget_usd: float | None = None,
                  notional_budget_usd: float | None = None) -> float:
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
    notional_cap = max(0.0, float(notional_budget_usd if notional_budget_usd is not None else
                                    os.getenv("FOREX_MAX_NOTIONAL_USD", "50")))
    margin_cap = max(0.0, float(margin_budget_usd if margin_budget_usd is not None else
                                  os.getenv("FOREX_MAX_MARGIN_USED_USD", "5")))
    absolute_notional = max(0.0, float(os.getenv("FOREX_ABSOLUTE_MAX_NOTIONAL_USD", "0") or 0))
    absolute_margin = max(0.0, float(os.getenv("FOREX_ABSOLUTE_MAX_MARGIN_USED_USD", "0") or 0))
    if absolute_notional:
        notional_cap = min(notional_cap, absolute_notional)
    if absolute_margin:
        margin_cap = min(margin_cap, absolute_margin)
    units = min(units, int(notional_cap / unit_notional_home), int(margin_cap / (unit_notional_home * margin_rate)))
    if units < 1:
        raise MultiAssetRejected("risk cap cannot support minimum unit")
    return float(units)


def confirmed_trade_actions(transactions: list[dict], summary: dict, open_trades: list[dict],
                            pending_orders: list[dict], risk: dict, snapshots: list[dict],
                            intents: list[dict], outcomes: list[dict]) -> list[dict]:
    """Convert confirmed broker fills into sanitized, explanatory email payloads."""
    actions = []
    intent_by_trade = {str(item.get("broker_trade_id")): item for item in intents
                       if item.get("broker_trade_id")}
    outcome_by_symbol = {str(item.get("symbol")): item for item in outcomes}
    calendar_verified = all(item.get("calendar_verified") is True for item in snapshots) if snapshots else False
    blackout = any(int(item.get("economic_event_within_minutes") or 0) > 0 for item in snapshots)

    def base(tx: dict, pair: str, action_id: str) -> dict:
        outcome = outcome_by_symbol.get(pair, {})
        score = outcome.get("score")
        signal = (
            f"Signal score {score:.2f} versus "
            f"{outcome.get('minimum_score', risk.get('minimum_score'))}; "
            f"{outcome.get('alignment', 'broker fill confirmation')}"
        ) if isinstance(score, (int, float)) else (
            "Confirmed broker fill; original signal details are retained in the trade ledger."
        )
        return {
            "action_id": action_id,
            "pair": pair,
            "execution_time": tx.get("time"),
            "resulting_unrealized_pnl_usd": summary.get("unrealized_pl"),
            "cumulative_realized_pnl_usd": summary.get("pl"),
            "daily_pnl_usd": (float(summary.get("nav") or 0) -
                              float(risk.get("daily_baseline_nav") or summary.get("nav") or 0)),
            "nav": summary.get("nav"),
            "margin_used": summary.get("margin_used"),
            "margin_available": summary.get("margin_available"),
            "remaining_positions": open_trades,
            "pending_protective_orders": pending_orders,
            "signal_trigger": signal,
            "calendar_state": (
                f"Verified: {'yes' if calendar_verified else 'no'}; "
                f"active/upcoming blackout evidence: {'yes' if blackout else 'no'}"
            ),
            "executor_state": "LIVE_ARMED and ready; broker fill confirmed",
            "risk_summary": (
                f"{float(risk.get('risk_per_trade_pct') or 0) * 100:.2f}% NAV per trade; "
                f"{float(risk.get('combined_risk_pct') or 0) * 100:.2f}% combined risk; "
                f"{risk.get('maximum_open_positions')} maximum positions; "
                f"{float(risk.get('drawdown_pct') or 0) * 100:.2f}% drawdown"
            ),
            "warnings": ([outcome.get("reason")] if outcome.get("reason") else []) +
                        (["Economic-calendar blackout is active for at least one scanned pair."]
                         if blackout else []),
        }

    for tx in transactions:
        if str(tx.get("type")) != "ORDER_FILL":
            continue
        tx_id = str(tx.get("id") or "")
        pair = str(tx.get("instrument") or "")
        reason = str(tx.get("reason") or "MARKET_ORDER")
        opened = tx.get("tradeOpened") or {}
        if opened:
            trade_id = str(opened.get("tradeID") or "")
            intent = intent_by_trade.get(trade_id, {})
            units = float(opened.get("units") or tx.get("units") or intent.get("quantity") or 0)
            side = "BUY" if units > 0 else "SELL"
            action = base(tx, pair or str(intent.get("symbol") or ""), f"{tx_id}:open:{trade_id}")
            action.update({
                "email_action": side,
                "action": "New position opened",
                "side": side,
                "filled_quantity": abs(units),
                "execution_price": opened.get("price") or tx.get("price") or intent.get("entry_price"),
                "realized_pnl_usd": float(tx.get("pl") or 0),
                "entry_reason": intent.get("entry_reason") or action["signal_trigger"],
                "trigger": (
                    f"A broker-confirmed {side} fill opened a new {action['pair'].replace('_', '/')} "
                    f"position. The order passed the strategy score, liquidity, spread, session, "
                    f"calendar, risk-budget, correlation, and protection checks."
                ),
                "position_impact": (
                    f"Exposure increased by {abs(units):g} units. "
                    f"{len(open_trades)} position(s) are now open, with protective stop-loss and "
                    f"take-profit orders listed below."
                ),
            })
            actions.append(action)
        closed_items = list(tx.get("tradesClosed") or [])
        if tx.get("tradeReduced"):
            closed_items.append(tx["tradeReduced"])
        for item in closed_items:
            trade_id = str(item.get("tradeID") or "")
            intent = intent_by_trade.get(trade_id, {})
            closed_pair = pair or str(intent.get("symbol") or "")
            units = abs(float(item.get("units") or 0))
            realized = (float(item.get("realizedPL") or 0) + float(item.get("financing") or 0) +
                        float(item.get("dividendAdjustment") or 0))
            trigger_names = {
                "STOP_LOSS_ORDER": "The protective stop-loss was executed because price reached the predefined loss boundary.",
                "TAKE_PROFIT_ORDER": "The protective take-profit was executed because price reached the planned profit target.",
                "MARKET_ORDER_TRADE_CLOSE": "The position was closed by a confirmed market close instruction.",
            }
            trigger = trigger_names.get(
                reason, f"A broker-confirmed fill reduced or closed the position ({reason.replace('_', ' ').lower()}).")
            action = base(tx, closed_pair, f"{tx_id}:close:{trade_id}")
            action.update({
                "email_action": "CLOSED",
                "action": "Partial position close" if tx.get("tradeReduced") and not tx.get("tradesClosed")
                          else "Position closed",
                "side": "CLOSE",
                "filled_quantity": units,
                "execution_price": item.get("price") or tx.get("price"),
                "realized_pnl_usd": realized,
                "entry_reason": intent.get("entry_reason") or "Historical broker trade: the original entry rationale was not stored.",
                "exit_reason": trigger,
                "trigger": trigger,
                "position_impact": (
                    f"Exposure decreased by {units:g} units. Realized result from this fill was "
                    f"{'a gain' if realized >= 0 else 'a loss'} of ${abs(realized):.4f}. "
                    f"{len(open_trades)} position(s) remain open."
                ),
            })
            actions.append(action)
    return actions


def five_streak_email_actions(outcomes: list[dict], closes: list[dict], intents: list[dict],
                              summary: dict) -> list[dict]:
    """Make Bryne and Lot-Bill paper fills first-class, idempotent email actions."""
    by_id = {str(item.get("id") or ""): item for item in intents}
    actions = []
    for item in outcomes:
        if item.get("status") != "PAPER_FILL":
            continue
        intent = by_id.get(str(item.get("intent_id") or ""), {})
        side = str(item.get("side") or intent.get("side") or "")
        reason = str(item.get("entry_reason") or intent.get("entry_reason") or
                     "Five qualifying closed M5 candles triggered the paper entry.")
        actions.append({
            "action_id": f"five-streak:v3:open:{item.get('intent_id')}",
            "strategy_name": FIVE_STREAK_DISPLAY_NAME,
            "email_action": f"PAPER {side}", "action": "New Bryne and Lot-Bill paper position opened",
            "pair": item.get("symbol"), "execution_time": item.get("signal_time"),
            "signal_time": item.get("signal_time") or intent.get("signal_time"), "side": side,
            "status": "PAPER_OPEN", "entry_price": item.get("entry") or intent.get("entry_price"),
            "stop_price": intent.get("stop_price"), "target_price": intent.get("target_price"),
            "maximum_loss_usd": intent.get("maximum_loss_usd"),
            "filled_quantity": intent.get("quantity"), "execution_price": item.get("entry"),
            "realized_pnl_usd": 0, "resulting_unrealized_pnl_usd": summary.get("unrealized_pl"),
            "nav": summary.get("nav"), "margin_used": summary.get("margin_used"),
            "margin_available": summary.get("margin_available"), "remaining_positions": [],
            "trigger": reason, "entry_reason": reason, "signal_trigger": reason,
            "position_impact": "Paper-only experiment; no broker funds or margin were used.",
            "calendar_state": "Paper experiment; market feed calendar evidence retained.",
            "executor_state": "PAPER ONLY", "risk_summary": "Filtered V3 paper risk cap; stop or 1.5R target only.",
            "warnings": ["Bryne and Lot-Bill Strategy is paper-only."],
        })
    for item in closes:
        if item.get("strategy") not in {FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY}:
            continue
        intent = by_id.get(str(item.get("intent_id") or ""), {})
        close_price = item.get("fill_price")
        price_detail = (f" at {close_price}" if close_price not in (None, "")
                        else "; the historical close price was not retained")
        exit_reason = f"Paper exit: {item.get('reason')}{price_detail}."
        actions.append({
            "action_id": f"five-streak:v3:close:{item.get('intent_id')}:{item.get('reason')}",
            "strategy_name": FIVE_STREAK_DISPLAY_NAME,
            "email_action": "PAPER CLOSED", "action": "Bryne and Lot-Bill paper position closed",
            "pair": item.get("symbol"), "execution_time": item.get("closed_at") or utcnow(),
            "signal_time": intent.get("signal_time"), "side": intent.get("side"),
            "status": "PAPER_CLOSED", "entry_price": intent.get("entry_price"),
            "stop_price": intent.get("stop_price"), "target_price": intent.get("target_price"),
            "maximum_loss_usd": intent.get("maximum_loss_usd"), "close_reason": item.get("reason"),
            "filled_quantity": intent.get("quantity"), "execution_price": item.get("fill_price"),
            "realized_pnl_usd": item.get("realized_pnl_usd"),
            "resulting_unrealized_pnl_usd": summary.get("unrealized_pl"), "nav": summary.get("nav"),
            "margin_used": summary.get("margin_used"), "margin_available": summary.get("margin_available"),
            "remaining_positions": [], "trigger": exit_reason,
            "entry_reason": intent.get("entry_reason") or "Historical paper entry reason was not stored.",
            "exit_reason": exit_reason, "signal_trigger": intent.get("entry_reason"),
            "position_impact": "Paper position closed; no broker funds or margin were used.",
            "calendar_state": "Paper experiment", "executor_state": "PAPER ONLY",
            "risk_summary": "Paper risk cap applied.",
            "warnings": ["Bryne and Lot-Bill Strategy is paper-only."],
        })
    return actions


class Executor:
    def __init__(self):
        self.adapter = OandaAdapter()
        path = os.getenv("FOREX_LEDGER_PATH", "/app/data/forex.sqlite3")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.ledger = Ledger(path)
        base_policy = AssetPolicy.from_env()
        self.engine = ForexEngine(replace(
            base_policy, minimum_score=float(os.getenv("FOREX_MIN_SCORE", "80"))))
        self.base_risk_pct = min(0.01, max(0.001, float(os.getenv("FOREX_RISK_PER_TRADE_PCT", "0.01"))))
        self.emailer = ForexReportEmailer(self.ledger)

    @staticmethod
    def max_positions() -> int:
        # Deliberate hard ceiling: NAV compounding may scale size, never concurrency beyond two.
        return min(2, max(1, int(os.getenv("FOREX_MAX_OPEN_POSITIONS", "2"))))

    @staticmethod
    def currencies(symbol: str) -> set[str]:
        return {part for part in str(symbol).upper().split("_") if part}

    def risk_limits(self, nav: float) -> dict:
        nav = max(0.0, float(nav))
        stored_peak_value = self.ledger.setting("peak_nav", "")
        stored_peak = float(stored_peak_value or nav)
        peak_nav = max(nav, stored_peak)
        if not stored_peak_value or peak_nav > stored_peak:
            self.ledger.set_setting("peak_nav", str(peak_nav))
        drawdown_pct = 0.0 if peak_nav <= 0 else max(0.0, (peak_nav - nav) / peak_nav)
        risk_pct, positions = self.base_risk_pct, self.max_positions()
        if drawdown_pct >= 0.10:
            risk_pct, positions = 0.0, 0
        elif drawdown_pct >= 0.07:
            risk_pct, positions = min(risk_pct, 0.0025), 1
        elif drawdown_pct >= 0.04:
            risk_pct = min(risk_pct, 0.005)
        return {
            "nav": nav,
            "peak_nav": peak_nav,
            "drawdown_pct": drawdown_pct,
            "risk_per_trade_pct": risk_pct,
            "risk_per_trade_usd": nav * risk_pct,
            "maximum_open_positions": positions,
            "combined_risk_pct": 0.02,
            "combined_risk_usd": nav * 0.02,
            "daily_loss_pct": 0.03,
            "daily_loss_usd": nav * 0.03,
            "weekly_loss_pct": 0.06,
            "weekly_loss_usd": nav * 0.06,
            "single_notional_pct": 1.0,
            "single_notional_usd": nav,
            "combined_notional_pct": 1.75,
            "combined_notional_usd": nav * 1.75,
            "maximum_margin_used_usd": nav * 0.10,
            "new_entries_halted": drawdown_pct >= 0.10,
        }

    def open_notional_home(self, trades: list[dict]) -> float:
        total = 0.0
        for trade in trades:
            symbol = str(trade.get("instrument") or "")
            units = abs(float(trade.get("currentUnits") or trade.get("initialUnits") or 0))
            if not symbol or units <= 0:
                continue
            quote = self.adapter.price(symbol)
            bids, asks = quote.get("bids", []), quote.get("asks", [])
            if not bids or not asks:
                continue
            mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
            factor = float(quote.get("quoteHomeConversionFactors", {}).get("negativeUnits") or 0)
            total += units * mid * factor
        return total

    def reconcile(self) -> dict:
        summary = self.adapter.preflight()
        trades = self.adapter.open_trades()
        pending = self.adapter.pending_orders()
        prior_tx = self.ledger.setting("last_transaction_id")
        transactions = self.adapter.transactions_since(prior_tx).get("transactions", []) if prior_tx else \
                       self.adapter.transactions_since("1").get("transactions", []) if trades else []
        if summary.get("last_transaction_id"):
            self.ledger.set_setting("last_transaction_id", summary["last_transaction_id"])
        self.ledger.event("BROKER_RECONCILIATION", {"summary": summary, "open_trades": trades,
                                                     "pending_orders": pending, "transactions": transactions})
        limits = self.risk_limits(float(summary["nav"]))
        if len(trades) > self.max_positions():
            raise BrokerError("broker position count exceeds two-position safety ceiling")
        if live_armed(self.adapter) or practice_armed(self.adapter):
            history_version = "tagged-closed-v1"
            if self.ledger.setting("historical_reconciliation_version") != history_version:
                history = self.adapter.transactions_since("1").get("transactions", [])
                imported = []
                mode = "LIVE" if live_armed(self.adapter) else "PRACTICE"
                for outcome in historical_managed_trade_outcomes(history, mode):
                    if self.ledger.import_closed_broker_intent(outcome):
                        imported.append({"intent_id": outcome["proposal_id"],
                                         "trade_id": outcome["broker_trade_id"],
                                         "symbol": outcome["symbol"],
                                         "realized_pnl_usd": outcome["realized_pnl_usd"]})
                self.ledger.set_setting("historical_reconciliation_version", history_version)
                self.ledger.event("BROKER_HISTORY_RECONCILED", {"imported": imported,
                                  "tagged_closed_trade_count": len(imported)})
            broker_positions = self.ledger.broker_positions()
            expected = {str(item.get("broker_trade_id") or "") for item in broker_positions if item.get("broker_trade_id")}
            actual = {str(item.get("id") or "") for item in trades}
            unexpected = actual - expected
            for trade in trades:
                trade_id = str(trade.get("id") or "")
                if trade_id not in unexpected:
                    continue
                limits = self.risk_limits(float(summary["nav"]))
                recovered = recoverable_managed_trade(trade, max(0.10, limits["risk_per_trade_usd"]), transactions)
                if not recovered:
                    continue
                existing=self.ledger.intent_for_broker_client_id(recovered["proposal_id"])
                intent_id=str(existing["id"]) if existing else recovered["proposal_id"]
                if not existing:
                    if self.ledger.has_intent(intent_id):
                        continue
                    self.ledger.add_intent(recovered, "LIVE" if live_armed(self.adapter) else "PRACTICE", "OPEN")
                self.ledger.update_intent(intent_id, "OPEN", trade_id=trade_id)
                self.ledger.event("BROKER_TRADE_RECOVERED", {"intent_id": intent_id,
                                  "trade_id": trade_id, "symbol": recovered["symbol"]})
                unexpected.remove(trade_id)
            broker_positions = self.ledger.broker_positions()
            if unexpected: raise BrokerError("unexpected broker trade detected; entries paused")
            self.ledger.update_excursions(trades)
            close_details = closed_trade_details(transactions)
            pnl_by_trade = {trade_id: float(detail["pnl"]) for trade_id,detail in close_details.items()}
            for position in broker_positions:
                trade_id = str(position.get("broker_trade_id") or "")
                if trade_id and trade_id not in actual:
                    pnl = pnl_by_trade.get(trade_id)
                    if pnl is None:
                        closed = self.adapter.trade(trade_id).get("trade", {})
                        pnl = (float(closed.get("realizedPL") or 0) + float(closed.get("financing") or 0)
                               + float(closed.get("dividendAdjustment") or 0))
                    close_reason=str((close_details.get(trade_id) or {}).get("reason") or "BROKER_HISTORY_LOOKUP")
                    if self.ledger.close_broker_intent(trade_id, pnl, close_reason):
                        self.ledger.event("BROKER_TRADE_CLOSED", {"intent_id": position["id"],
                                          "trade_id": trade_id, "realized_pnl_usd": pnl,"close_reason":close_reason})
            for trade in trades:
                if not trade.get("stopLossOrder") or not trade.get("takeProfitOrder"):
                    trade_id = str(trade.get("id") or "")
                    result = self.adapter.close_trade(trade_id) if trade_id else {}
                    self.ledger.event("UNPROTECTED_TRADE_EMERGENCY_CLOSE", {"trade_id": trade_id, "response": result})
                    raise BrokerError("unprotected broker trade closed; entries paused")
        today = datetime.now(UTC).date()
        today_key = today.isoformat()
        week_key = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
        if self.ledger.setting("daily_baseline_date") != today_key:
            self.ledger.set_setting("daily_baseline_date", today_key)
            self.ledger.set_setting("daily_baseline_nav", str(summary["nav"]))
            self.ledger.event("DAILY_BASELINE_RESET", {"date": today_key, "nav": summary["nav"]})
        if self.ledger.setting("weekly_baseline_week") != week_key:
            self.ledger.set_setting("weekly_baseline_week", week_key)
            self.ledger.set_setting("weekly_baseline_nav", str(summary["nav"]))
            self.ledger.event("WEEKLY_BASELINE_RESET", {"week": week_key, "nav": summary["nav"]})
        daily_nav = float(self.ledger.setting("daily_baseline_nav", str(summary["nav"])))
        weekly_nav = float(self.ledger.setting("weekly_baseline_nav", str(summary["nav"])))
        armed = live_armed(self.adapter) or practice_armed(self.adapter)
        if armed and daily_nav - float(summary["nav"]) >= limits["daily_loss_usd"]:
            raise BrokerError("3% daily NAV loss circuit breaker active")
        if armed and weekly_nav - float(summary["nav"]) >= limits["weekly_loss_usd"]:
            raise BrokerError("6% weekly NAV loss circuit breaker active")
        if armed and limits["new_entries_halted"]:
            raise BrokerError("10% peak-to-current drawdown circuit breaker active")
        if armed and float(summary["margin_used"]) > limits["maximum_margin_used_usd"]:
            raise BrokerError("NAV-based margin-used circuit breaker active")
        with LOCK:
            STATE["open_positions"] = len(trades)
        return {"summary": summary, "open_trades": trades, "pending_orders": pending,
                "transactions": transactions}

    def process(self, snapshot: dict) -> dict:
        proposal = vars(self.engine.evaluate(snapshot))
        proposal["model_version"] = FOREX_MODEL_VERSION
        alignment, _, _ = self.engine.alignment(snapshot)
        proposal["entry_reason"] = (
            f"{alignment.replace('_', ' ').title()} {proposal['side']} signal; "
            f"score {float(proposal['score']):.2f} met minimum {self.engine.policy.minimum_score:.2f}; "
            f"horizon agreement {float(proposal.get('horizon_agreement') or 0) * 100:.1f}%; "
            f"estimated net value {float(proposal.get('expected_net_bps') or 0):.2f} bps."
        )
        calendar_ok = snapshot.get("calendar_verified") is True and str(snapshot.get("economic_event_source", "")).startswith("https://")
        mode = "LIVE" if live_armed(self.adapter) else "PRACTICE" if practice_armed(self.adapter) else "PAPER_ONLY"
        if mode in {"LIVE", "PRACTICE"} and not calendar_ok:
            raise MultiAssetRejected("verified economic calendar evidence required for broker execution")
        preflight = self.adapter.preflight()
        limits = self.risk_limits(float(preflight.get("nav") or preflight.get("balance") or 0))
        if limits["new_entries_halted"]:
            raise MultiAssetRejected("10% drawdown throttle halts new entries")
        if self.ledger.open_count() >= limits["maximum_open_positions"]:
            raise MultiAssetRejected("position limit reached")
        overlaps = [symbol for symbol in self.ledger.open_symbols()
                    if self.currencies(symbol) & self.currencies(str(proposal["symbol"]))]
        if overlaps:
            raise MultiAssetRejected("currency/correlation overlap guard active")
        if self.ledger.symbol_in_cooldown(str(proposal["symbol"]), int(os.getenv("FOREX_SYMBOL_COOLDOWN_SECONDS", "3600"))):
            raise MultiAssetRejected("symbol cooldown active")
        remaining_risk = limits["combined_risk_usd"] - self.ledger.open_risk()
        trade_risk = min(limits["risk_per_trade_usd"], remaining_risk)
        if trade_risk < max(0.01, float(os.getenv("FOREX_MIN_EXECUTABLE_RISK_USD", "0.10"))):
            raise MultiAssetRejected("combined portfolio risk cap reached")
        proposal["maximum_loss_usd"] = trade_risk
        intent_id = proposal["proposal_id"]
        if self.ledger.has_intent(intent_id):
            return {"status": "DUPLICATE_SUPPRESSED", "id": intent_id}
        open_trades = self.adapter.open_trades() if mode != "PAPER_ONLY" else []
        open_notional = self.open_notional_home(open_trades) if open_trades else 0.0
        notional_remaining = limits["combined_notional_usd"] - open_notional
        single_notional = min(limits["single_notional_usd"], notional_remaining)
        margin_remaining = limits["maximum_margin_used_usd"] - float(preflight.get("margin_used") or 0)
        if single_notional <= 0:
            raise MultiAssetRejected("combined NAV-based notional cap reached")
        if margin_remaining <= 0:
            raise MultiAssetRejected("combined NAV-based margin cap reached")
        proposal["quantity"] = safe_quantity(
            self.adapter, proposal, trade_risk, margin_remaining, single_notional)
        if mode == "PAPER_ONLY":
            self.ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
            self.ledger.event("PAPER_FILL", proposal)
            return {"status": "PAPER_FILL", "id": intent_id}
        if preflight["balance"] <= 0 or preflight["margin_available"] <= 0:
            raise MultiAssetRejected("live account has no available capital")
        if preflight["open_trade_count"] >= limits["maximum_open_positions"]:
            raise MultiAssetRejected("broker position limit reached")
        self.ledger.add_intent(proposal, mode, "SUBMITTING")
        response = self.adapter.create_order(proposal, client_order_id=broker_client_id(intent_id))
        rejected = response.get("orderRejectTransaction")
        if rejected:
            self.ledger.update_intent(intent_id, "REJECTED")
            self.ledger.event("ORDER_REJECTED", {"intent_id": intent_id, "response": response})
            raise BrokerError(str(rejected.get("rejectReason") or "order rejected"))
        cancelled = response.get("orderCancelTransaction")
        if cancelled and not response.get("orderFillTransaction"):
            self.ledger.update_intent(intent_id, "CANCELLED")
            self.ledger.event("ORDER_CANCELLED", {"intent_id": intent_id, "response": response})
            raise BrokerError(str(cancelled.get("reason") or "order cancelled without fill"))
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
        marks = {str(item.get("symbol")): item for item in snapshots}
        closes = []
        for position in self.ledger.paper_positions():
            snapshot = marks.get(position["symbol"], {})
            # A long can be sold at bid; a short must be bought back at ask.
            executable = snapshot.get("bid") if position["side"] == "BUY" else snapshot.get("ask")
            price = float(executable or snapshot.get("price") or 0)
            if price <= 0: continue
            side = position["side"]; stop = float(position["stop_price"]); target = float(position["target_price"])
            current_pnl = five_streak_position_pnl(position, price) if position.get("strategy") in {FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY} else 0.0
            with self.ledger.db:
                self.ledger.db.execute("""UPDATE intents SET
                    max_favorable_pnl_usd=MAX(COALESCE(max_favorable_pnl_usd,0),?),
                    max_adverse_pnl_usd=MIN(COALESCE(max_adverse_pnl_usd,0),?) WHERE id=?""",
                    (current_pnl, current_pnl, position["id"]))
            reason = "STOP" if (side == "BUY" and price <= stop) or (side == "SELL" and price >= stop) else \
                     "TARGET" if (side == "BUY" and price >= target) or (side == "SELL" and price <= target) else ""
            if reason:
                direction = 1 if side == "BUY" else -1
                if position.get("strategy") in {FIVE_STREAK_STRATEGY, FIVE_STREAK_FILTERED_STRATEGY}:
                    pnl = five_streak_position_pnl(position, price)
                else:
                    pnl = (price - float(position["entry_price"])) * float(position["quantity"]) * direction
                self.ledger.update_intent(position["id"], "PAPER_CLOSED")
                with self.ledger.db:
                    self.ledger.db.execute("UPDATE intents SET realized_pnl_usd=?,closed_at=?,close_reason=?,close_price=? WHERE id=?",
                                           (round(pnl, 8), utcnow(), reason, price, position["id"]))
                event = {"intent_id": position["id"], "symbol": position["symbol"], "fill_price": price,
                         "reason": reason, "realized_pnl_usd": round(pnl, 8),
                         "strategy": position.get("strategy"), "closed_at": utcnow()}
                self.ledger.event("PAPER_CLOSE", event); closes.append(event)
        return closes

    def process_five_streak_paper(self, snapshots: list[dict], nav: float) -> list[dict]:
        if not five_streak_enabled():
            return []
        outcomes = []
        max_open = max(1, min(8, int(os.getenv("FOREX_FIVE_STREAK_MAX_OPEN", "8"))))
        per_trade_risk = max(.01, nav * min(.0025, max(.0005, float(os.getenv(
            "FOREX_FIVE_STREAK_RISK_PCT", ".0025")))))
        total_risk_cap = nav * .02
        for snapshot in snapshots:
            try:
                signals = five_streak_signals(snapshot)
            except Exception as exc:
                outcomes.append({"symbol": snapshot.get("symbol"), "status": "REJECTED", "reason": str(exc)[:300]})
                continue
            if not signals:
                outcomes.append({"symbol": snapshot.get("symbol"), "status": "NO_SIGNAL",
                                 "reason": "newest closed M5 candle has no qualifying five-candle streak"})
            for proposal in signals:
                if self.ledger.has_intent(proposal["proposal_id"]):
                    continue
                same_symbol = self.ledger.db.execute(
                    "SELECT 1 FROM intents WHERE strategy=? AND symbol=? AND status='PAPER_OPEN' LIMIT 1",
                    (FIVE_STREAK_FILTERED_STRATEGY, proposal["symbol"])).fetchone()
                if same_symbol or self.ledger.symbol_in_cooldown(proposal["symbol"], 3600):
                    outcomes.append({"symbol": proposal["symbol"], "status": "COOLDOWN_REJECTED",
                                     "reason": "Filtered V3 permits one position per symbol and a 60-minute cooldown"})
                    continue
                row = self.ledger.db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(maximum_loss_usd),0) FROM intents WHERE strategy=? AND status='PAPER_OPEN'",
                    (FIVE_STREAK_FILTERED_STRATEGY,)).fetchone()
                if int(row[0]) >= max_open or float(row[1]) + per_trade_risk > total_risk_cap:
                    outcomes.append({"symbol": proposal["symbol"], "status": "RISK_CAP_REJECTED",
                                     "signal_time": proposal["signal_time"]})
                    continue
                proposal["maximum_loss_usd"] = per_trade_risk
                proposal.setdefault("entry_reason", "Filtered V3 five-candle entry with recorded market evidence.")
                self.ledger.add_intent(proposal, "PAPER_ONLY", "PAPER_OPEN")
                self.ledger.event("FIVE_STREAK_PAPER_FILL", proposal)
                outcomes.append({"symbol": proposal["symbol"], "side": proposal["side"],
                                 "status": "PAPER_FILL", "signal_time": proposal["signal_time"],
                                 "intent_id": proposal["proposal_id"], "entry_reason": proposal["entry_reason"],
                                 "entry": proposal["reference_price"], "stop": proposal["stop_price"],
                                 "target": proposal["target_price"], "maximum_loss_usd": per_trade_risk})
        return outcomes

    def scan(self) -> None:
        if not 0 <= self.engine.policy.minimum_score <= 100:
            raise BrokerError(f"FOREX_MIN_SCORE must be between 0 and 100; got {self.engine.policy.minimum_score:g}")
        reconciliation = self.reconcile()
        payload = fetch_json(os.environ["MULTI_ASSET_FEED_URL"])
        snapshots = validated_snapshots(payload)
        closes = self.supervise_paper(snapshots)
        five_streak_outcomes = self.process_five_streak_paper(
            snapshots, float(reconciliation["summary"]["nav"]))
        outcomes = []
        for snapshot in snapshots:
            score = round(self.engine.score(snapshot), 2)
            alignment, alignment_points, proposed_side = self.engine.alignment(snapshot)
            diagnostics = {"alignment": alignment, "alignment_points": alignment_points,
                           "proposed_side": proposed_side,
                           "horizon_agreement": snapshot.get("horizon_agreement"),
                           "liquidity_score": snapshot.get("liquidity_score"),
                           "atr_14": snapshot.get("atr_14"),
                           "ewma_volatility_price": snapshot.get("ewma_volatility_price")}
            try:
                outcomes.append({"symbol": snapshot.get("symbol"), "score": score,
                                 "minimum_score": self.engine.policy.minimum_score, **diagnostics,
                                 **self.process(snapshot)})
            except Exception as exc:
                outcomes.append({"symbol": snapshot.get("symbol"), "score": score,
                                 "minimum_score": self.engine.policy.minimum_score,
                                 **diagnostics,
                                 "status": "REJECTED", "reason": str(exc)[:300]})
        self.ledger.event("SCAN", {"outcomes": outcomes, "paper_closes": closes})
        report = {"generated_at": utcnow(), "deployment": deployment_info(), "mode": "LIVE_ARMED" if live_armed(self.adapter) else
                  "PRACTICE_ARMED" if practice_armed(self.adapter) else "PAPER_ONLY",
                  "executor_ready": True, "last_scan": utcnow(), "last_error": "",
                  "broker": reconciliation["summary"],
                  "open_trade_count": len(reconciliation["open_trades"]),
                  "pending_order_count": len(reconciliation["pending_orders"]),
                  "transaction_count_since_prior_scan": len(reconciliation["transactions"]),
                  "snapshots": snapshots, "outcomes": outcomes, "paper_closes": closes,
                  "five_streak": {"name": FIVE_STREAK_DISPLAY_NAME, "mode": "PAPER_ONLY",
                                  "version": "Filtered V3", "timeframe": "M5",
                                  "enabled": five_streak_enabled(),
                                  "exit_policy": "stop or 1.5R target only; no arbitrary time exit",
                                  "cost_model": "closed M5 signal; executable bid/ask entry and exit",
                                  "outcomes": five_streak_outcomes,
                                  "performance": {**self.ledger.strategy_stats(FIVE_STREAK_FILTERED_STRATEGY),
                                      "unrealized_pnl_usd": round(sum(
                                          five_streak_position_pnl(position, next((float(s.get("price") or 0)
                                              for s in snapshots if s.get("symbol") == position.get("symbol")), 0))
                                          for position in self.ledger.paper_positions()
                                          if position.get("strategy") == FIVE_STREAK_FILTERED_STRATEGY), 8)},
                                  "trades": self.ledger.strategy_intents(FIVE_STREAK_FILTERED_STRATEGY),
                                  "baseline_v2_archived": {"new_entries_enabled": False,
                                      "performance": self.ledger.strategy_stats(FIVE_STREAK_STRATEGY),
                                      "trades": self.ledger.strategy_intents(FIVE_STREAK_STRATEGY)}},
                  "intents": self.ledger.recent_intents(), "events": self.ledger.recent_events(),
                  "realized_pnl_usd": self.ledger.realized_pnl(),
                  "model_review": self.ledger.model_review(self.engine.policy.minimum_score),
                  "risk_configuration": {
                      "minimum_score": self.engine.policy.minimum_score,
                      "daily_baseline_nav": float(self.ledger.setting(
                          "daily_baseline_nav", str(reconciliation["summary"]["nav"]))),
                      **self.risk_limits(float(reconciliation["summary"]["nav"])),
                      "grandfathered_positions_above_limit": max(
                          0, len(reconciliation["open_trades"]) -
                          self.risk_limits(float(reconciliation["summary"]["nav"]))["maximum_open_positions"]),
                      "current_open_risk_usd": self.ledger.open_risk(),
                      "current_open_notional_usd": self.open_notional_home(reconciliation["open_trades"]),
                      "currency_overlap_guard": True,
                      "correlation_guard": "shared base or quote currency prohibited",
                      "minimum_reward_risk_ratio": 2.0,
                      "high_impact_calendar_blackout": True,
                  },
                  "capital_baseline_nav": float(os.getenv("FOREX_LIVE_BASELINE_USD", "0") or 0),
                  "daily_baseline_nav": float(self.ledger.setting("daily_baseline_nav", str(reconciliation["summary"]["nav"])))}
        trade_actions = confirmed_trade_actions(
            reconciliation["transactions"], reconciliation["summary"],
            reconciliation["open_trades"], reconciliation["pending_orders"],
            report["risk_configuration"], snapshots, report["intents"], outcomes)
        trade_actions.extend(five_streak_email_actions(
            five_streak_outcomes, closes, report["intents"], reconciliation["summary"]))
        # Versioned handoff: include the latest closed paper result on every
        # scan. The emailer's persisted action IDs make this a one-time,
        # restart-safe backfill and prevent repeated historical reports.
        latest_five_closed = next((item for item in report["five_streak"]["trades"]
                                   if item.get("status") == "PAPER_CLOSED"), None)
        if latest_five_closed:
            trade_actions.extend(five_streak_email_actions([], [{
                "intent_id": latest_five_closed.get("id"),
                "symbol": latest_five_closed.get("symbol"),
                "fill_price": latest_five_closed.get("close_price"),
                "reason": latest_five_closed.get("close_reason"),
                "realized_pnl_usd": latest_five_closed.get("realized_pnl_usd"),
                "strategy": FIVE_STREAK_FILTERED_STRATEGY,
                "closed_at": latest_five_closed.get("closed_at"),
            }], report["intents"], reconciliation["summary"]))
        delivery = self.emailer.status()
        delivery_payload = dict(report)
        delivery_payload["_trade_actions"] = trade_actions
        delivery["attempt"] = self.emailer.maybe_send(delivery_payload)
        delivery["confirmed_actions_this_scan"] = len(trade_actions)
        report["email_delivery"] = delivery
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
