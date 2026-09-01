from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHECKPOINTS = (5, 15, 30, 60, 120, 240)
HARD_RISK_TERMS = (
    "no sell route", "unsellable", "sell impact", "excessive impact",
    "sell simulation failed", "price impact above", "mint authority",
    "freeze authority", "unsafe", "stale", "honeypot",
)
SOFT_RISK_TERMS = (
    "momentum", "acceleration", "retracement", "market cap", "volume",
    "buy pressure", "buyer", "age", "timing", "confirmation", "score",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def first(item: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if item.get(key) is not None:
            return item[key]
    return default


def failures(item: dict) -> list[str]:
    value = first(item, "paper_failures", "failures", "blocking_reasons",
                  "rejection_reasons", "blockers", default=[])
    if isinstance(value, str):
        return [value]
    return [str(entry) for entry in (value or []) if entry]


def classify_cohort(item: dict) -> tuple[str, list[str], list[str]]:
    rejected = failures(item)
    lowered = [entry.lower() for entry in rejected]
    hard = [entry for entry, low in zip(rejected, lowered)
            if any(term in low for term in HARD_RISK_TERMS)]
    soft = [entry for entry, low in zip(rejected, lowered)
            if any(term in low for term in SOFT_RISK_TERMS)]
    if item.get("qualified") is True or item.get("paper_qualified") is True:
        return "PRODUCTION_QUALIFIED" if item.get("live_eligible") else "PAPER_QUALIFIED", hard, soft
    if hard:
        return "HARD_REJECT_CONTROL", hard, soft
    if len(rejected) == 1 and soft:
        return "NEAR_MISS_SHADOW", hard, soft
    if rejected and len(rejected) <= 2 and len(soft) == len(rejected):
        return "RESEARCH_SHADOW", hard, soft
    return "REJECTED_CONTROL", hard, soft


class IntelligenceLedger:
    """Persistent, append-only evidence warehouse spanning every strategy.

    It records source payloads verbatim for reproducibility, while normalized
    columns make cross-strategy comparisons deterministic. It never changes a
    strategy or authorizes a trade.
    """

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS observations(
              evidence_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL,
              observed_at TEXT, source_service TEXT NOT NULL,
              source_url TEXT, asset_class TEXT NOT NULL,
              strategy TEXT NOT NULL, strategy_version TEXT,
              cohort TEXT NOT NULL, mode TEXT NOT NULL,
              instrument TEXT, contract_address TEXT, pool_address TEXT,
              event_type TEXT NOT NULL, decision TEXT,
              side TEXT, entry_price REAL, exit_price REAL,
              entry_amount_usd REAL, realized_pnl_usd REAL,
              cost_stressed_pnl_usd REAL, fees_usd REAL,
              mfe_usd REAL, mae_usd REAL, hold_minutes REAL,
              hard_failures_json TEXT NOT NULL, soft_failures_json TEXT NOT NULL,
              payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS observations_strategy_idx
              ON observations(strategy,strategy_version,observed_at);
            CREATE INDEX IF NOT EXISTS observations_instrument_idx
              ON observations(asset_class,instrument,observed_at);
            CREATE TABLE IF NOT EXISTS checkpoints(
              evidence_id TEXT NOT NULL, checkpoint_minutes INTEGER NOT NULL,
              observed_at TEXT, executable_price REAL,
              executable_pnl_usd REAL, mark_return REAL,
              sell_route_ok INTEGER, sell_impact_bps REAL,
              liquidity_usd REAL, spread_bps REAL, slippage_bps REAL,
              payload_json TEXT NOT NULL,
              PRIMARY KEY(evidence_id,checkpoint_minutes));
            CREATE TABLE IF NOT EXISTS rule_evidence(
              evidence_id TEXT NOT NULL, rule TEXT NOT NULL,
              severity TEXT NOT NULL, passed INTEGER NOT NULL,
              value_json TEXT, PRIMARY KEY(evidence_id,rule,severity));
            CREATE TABLE IF NOT EXISTS learnings(
              learning_id TEXT PRIMARY KEY, learning_key TEXT NOT NULL,
              created_at TEXT NOT NULL, strategy TEXT NOT NULL,
              mechanism TEXT NOT NULL, status TEXT NOT NULL,
              statement TEXT NOT NULL, sample_size INTEGER NOT NULL,
              wins INTEGER NOT NULL, expectancy_usd REAL,
              cost_stressed_expectancy_usd REAL, evidence_ids_json TEXT NOT NULL,
              adoption_threshold TEXT NOT NULL, payload_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS learnings_key_idx
              ON learnings(learning_key,created_at);
            CREATE TABLE IF NOT EXISTS ingestion_runs(
              run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
              completed_at TEXT, sources_json TEXT NOT NULL,
              observations_added INTEGER NOT NULL DEFAULT 0,
              checkpoints_added INTEGER NOT NULL DEFAULT 0,
              learnings_added INTEGER NOT NULL DEFAULT 0,
              error TEXT);
            """)

    @staticmethod
    def evidence_id(service: str, strategy: str, item: dict, ordinal: int = 0) -> str:
        identity = first(item, "id", "action_id", "ticket_id", "proposal_id", "trade_id",
                         "signal_id", "signature", "key")
        if not identity:
            identity = ":".join(str(first(item, key, default="")) for key in
                                ("mint", "instrument", "symbol", "product_id", "at", "created_at", "observed_at"))
        body = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(f"{service}|{strategy}|{identity}|{ordinal}|{body}".encode()).hexdigest()

    def record(self, service: str, asset_class: str, strategy: str, item: dict,
               *, event_type: str, mode: str, source_url: str = "", ordinal: int = 0) -> tuple[str, bool, int]:
        cohort, hard, soft = classify_cohort(item)
        evidence_id = self.evidence_id(service, strategy, item, ordinal)
        body = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
        instrument = first(item, "instrument", "symbol", "pair", "product_id", "mint")
        status = str(first(item, "status", "decision", "action", default=""))
        realized = number(first(item, "realized_pnl_usd", "realizedPnlUsd", "net_pnl_usd",
                                "pnl_usd", "pnlVsEntryUsd"))
        stressed = number(first(item, "cost_stressed_pnl_usd", "costStressedPnlUsd"))
        values = (
            evidence_id, utcnow(), first(item, "observed_at", "at", "created_at", "signal_time", "closed_at"),
            service, source_url, asset_class, strategy,
            str(first(item, "strategy_version", "strategyVersion", "version", default="")),
            cohort, mode, str(instrument or ""), str(first(item, "mint", "contract_address", default="")),
            str(first(item, "pool", "pool_address", default="")), event_type, status,
            str(first(item, "side", default="")), number(first(item, "entry_price", "entryPrice", "reference_price")),
            number(first(item, "exit_price", "close_price", "executable_price")),
            number(first(item, "entry_amount_usd", "entryUsd", "inputUsd", "notional_usd", "maximum_loss_usd")),
            realized, stressed, number(first(item, "fees_usd", "networkFeeUsd", "commission_usd")),
            number(first(item, "max_favorable_pnl_usd", "mfe_usd", "maximumFavorablePnlUsd")),
            number(first(item, "max_adverse_pnl_usd", "mae_usd", "maximumAdversePnlUsd")),
            number(first(item, "hold_minutes", "duration_minutes")), json.dumps(hard), json.dumps(soft),
            body, hashlib.sha256(body.encode()).hexdigest(),
        )
        with self.lock, self.db:
            cursor = self.db.execute(
                f"INSERT OR IGNORE INTO observations VALUES({','.join('?' for _ in values)})", values)
            for rule in hard:
                self.db.execute("INSERT OR IGNORE INTO rule_evidence VALUES(?,?,?,?,?)",
                                (evidence_id, rule, "HARD", 0, "null"))
            for rule in soft:
                self.db.execute("INSERT OR IGNORE INTO rule_evidence VALUES(?,?,?,?,?)",
                                (evidence_id, rule, "SOFT", 0, "null"))
            checkpoint_added = self._record_checkpoints(evidence_id, item)
        return evidence_id, cursor.rowcount == 1, checkpoint_added

    def _record_checkpoints(self, evidence_id: str, item: dict) -> int:
        checkpoints = item.get("checkpoints") or {}
        added = 0
        if isinstance(checkpoints, list):
            checkpoints = {str(first(row, "checkpoint_minutes", "minutes")): row for row in checkpoints}
        for key, row in checkpoints.items() if isinstance(checkpoints, dict) else []:
            if not isinstance(row, dict):
                continue
            try:
                minute = int(str(key).lower().replace("m", ""))
            except ValueError:
                continue
            if minute not in CHECKPOINTS:
                continue
            payload = json.dumps(row, sort_keys=True, default=str)
            values = (
                evidence_id, minute, first(row, "observed_at", "observedAt"),
                number(first(row, "executable_price", "price", "price_usd")),
                number(first(row, "executable_pnl_usd", "executablePnlUsd", "pnl_usd", "pnlVsEntryUsd")),
                number(first(row, "mark_return", "return_pct")),
                None if first(row, "sell_route_ok", "sellRouteOk") is None else
                    int(bool(first(row, "sell_route_ok", "sellRouteOk"))),
                number(first(row, "sell_price_impact_bps", "sellImpactBps")),
                number(first(row, "liquidity_usd", "liquidityUsd")),
                number(first(row, "spread_bps", "spreadBps")),
                number(first(row, "slippage_bps", "slippageBps")), payload)
            cursor = self.db.execute(
                f"INSERT OR IGNORE INTO checkpoints VALUES({','.join('?' for _ in values)})", values)
            added += int(cursor.rowcount == 1)
        return added

    @staticmethod
    def _collections(snapshot: dict) -> list[tuple[str, str, str, str, str, list[dict]]]:
        coinbase, forex = snapshot.get("coinbase") or {}, snapshot.get("forex") or {}
        solana, discovery = snapshot.get("solana") or {}, snapshot.get("discovery") or {}
        collections: list[tuple[str, str, str, str, str, list[dict]]] = []
        collections.append(("coinbase", "CRYPTO", "COINBASE_MEME_LIVE", "TRADE_OR_DECISION", "LIVE",
                            list(coinbase.get("recommendations") or [])))
        collections.append(("forex", "FOREX", "FOREX_CONTROL", "TRADE_INTENT", "LIVE",
                            list(forex.get("intents") or [])))
        collections.append(("forex", "FOREX", "FOREX_CONTROL", "BROKER_ACTION", "LIVE",
                            list(forex.get("recentActions") or [])))
        collections.append(("forex", "FOREX", "FOREX_CONTROL", "CANDIDATE_DECISION", "SHADOW",
                            list(forex.get("outcomes") or [])))
        collections.append(("forex", "FOREX", "FOREX_CONTROL", "TRADE_CHECKPOINT", "SHADOW",
                            list(forex.get("trade_checkpoints") or []) +
                            list(forex.get("live_trade_checkpoints") or [])))
        five = forex.get("five_streak") or {}
        collections.append(("forex", "FOREX", str(five.get("strategy") or "BRYNE_LOT_BILL_V5"),
                            "PAPER_TRADE", "PAPER", list(five.get("trades") or five.get("positions") or [])))
        for archive_key, default_strategy in (
            ("v4_ratchet_archived", "BRYNE_LOT_BILL_V4"),
            ("filtered_v3_archived", "BRYNE_LOT_BILL_V3"),
            ("baseline_v2_archived", "BRYNE_LOT_BILL_V2"),
        ):
            archive = five.get(archive_key) or {}
            collections.append(("forex", "FOREX", default_strategy, "ARCHIVED_PAPER_TRADE", "PAPER",
                                list(archive.get("trades") or [])))
        collections.append(("forex", "FOREX", "BRYNE_LOT_BILL_V5", "PAPER_CLOSE", "PAPER",
                            list(forex.get("paper_closes") or [])))
        collections.append(("solana", "CRYPTO", "SOLANA_ALL", "PAPER_OR_LIVE_ACTION", "MIXED",
                            list(solana.get("paperFills") or solana.get("recentActions") or [])))
        live_probe = solana.get("runnerLiveProbe") or {}
        collections.append(("solana", "CRYPTO", "SOLANA_MICROCAP_RUNNER_LIVE_PROBE", "LIVE_PROBE", "LIVE",
                            list(live_probe.get("fills") or [])))
        collections.append(("solana", "CRYPTO", "SOLANA_RUNNER_FOLLOWUP", "POST_EXIT_PATH", "SHADOW",
                            list(solana.get("postExitFollowups") or solana.get("postExitCounterfactuals") or [])))
        for item in discovery.get("candidates") or []:
            strategy = str(item.get("strategy") or "SOLANA_DISCOVERY")
            collections.append(("discovery", "CRYPTO", strategy, "CANDIDATE_DECISION", "SHADOW", [item]))
        for strategy, items in (discovery.get("strategy_watchlists") or {}).items():
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                latest = item.get("latest_candidate") or {}
                retained = {**latest, **item, "strategy": strategy,
                            "event_type": "SHADOW_FORWARD_PATH"}
                collections.append(("discovery", "CRYPTO", str(strategy),
                                    "SHADOW_FORWARD_PATH", "SHADOW", [retained]))
        return collections

    def ingest_snapshot(self, snapshot: dict) -> dict:
        started = utcnow()
        run_id = hashlib.sha256(f"{started}|{snapshot.get('generated_at')}".encode()).hexdigest()
        sources = snapshot.get("sources") or {}
        with self.lock, self.db:
            self.db.execute("INSERT INTO ingestion_runs(run_id,started_at,sources_json) VALUES(?,?,?)",
                            (run_id, started, json.dumps(sources, sort_keys=True)))
        observations_added = checkpoints_added = 0
        error = ""
        try:
            for service, asset_class, default_strategy, event_type, mode, items in self._collections(snapshot):
                source_url = str((sources.get(service) or {}).get("source") or "")
                for ordinal, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    strategy = str(item.get("strategy") or default_strategy)
                    _, inserted, checkpoint_count = self.record(
                        service, asset_class, strategy, item, event_type=event_type,
                        mode=mode, source_url=source_url, ordinal=ordinal)
                    observations_added += int(inserted)
                    checkpoints_added += checkpoint_count
            learnings_added = self.extract_learnings()
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
            learnings_added = 0
        with self.lock, self.db:
            self.db.execute("""UPDATE ingestion_runs SET completed_at=?,observations_added=?,
                checkpoints_added=?,learnings_added=?,error=? WHERE run_id=?""",
                (utcnow(), observations_added, checkpoints_added, learnings_added, error, run_id))
        return {"run_id": run_id, "observations_added": observations_added,
                "checkpoints_added": checkpoints_added, "learnings_added": learnings_added,
                "error": error}

    def extract_learnings(self) -> int:
        rows = [dict(row) for row in self.db.execute("""SELECT evidence_id,strategy,cohort,
            realized_pnl_usd,cost_stressed_pnl_usd FROM observations
            WHERE realized_pnl_usd IS NOT NULL""").fetchall()]
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[row["strategy"]].append(row)
        added = 0
        for strategy, values in grouped.items():
            pnl = [float(row["realized_pnl_usd"]) for row in values]
            stressed = [float(row["cost_stressed_pnl_usd"] if row["cost_stressed_pnl_usd"] is not None
                              else row["realized_pnl_usd"]) for row in values]
            wins = sum(value > 0 for value in stressed)
            expectancy = sum(pnl) / len(pnl)
            stressed_expectancy = sum(stressed) / len(stressed)
            if len(values) >= 100 and stressed_expectancy > 0:
                status, statement = "ELIGIBLE_FOR_REVIEW", (
                    f"{strategy} reached 100 cost-stressed observations with positive expectancy; "
                    "manual promotion review is permitted, not automatic.")
            elif stressed_expectancy > 0:
                status, statement = "PROMISING_INSUFFICIENT_SAMPLE", (
                    f"{strategy} has positive cost-stressed expectancy, but only {len(values)}/100 "
                    "observations; continue paper or shadow collection.")
            else:
                status, statement = "DO_NOT_PROMOTE", (
                    f"{strategy} has non-positive cost-stressed expectancy across {len(values)} "
                    "observations; retain as research and compare entry/exit mechanisms.")
            added += self._learning(strategy, "COST_STRESSED_EXPECTANCY", status, statement,
                                    len(values), wins, expectancy, stressed_expectancy,
                                    [row["evidence_id"] for row in values[-100:]],
                                    "100 independent positive cost-stressed closes for model/risk promotion")
        route_rows = [dict(row) for row in self.db.execute("""SELECT o.evidence_id,o.strategy,
            o.realized_pnl_usd,c.sell_route_ok FROM observations o JOIN checkpoints c
            ON c.evidence_id=o.evidence_id WHERE c.sell_route_ok=0""").fetchall()]
        if route_rows:
            losses = [row for row in route_rows if number(row["realized_pnl_usd"], 0) < 0]
            statement = (f"Sell-route failure appeared in {len(route_rows)} executable checkpoints; "
                         f"{len(losses)} had confirmed losses. Keep sellability as a hard gate.")
            added += self._learning("ALL_CRYPTO", "EXECUTABLE_SELLABILITY", "STRUCTURAL_HARD_GATE",
                                    statement, len(route_rows), 0, None, None,
                                    [row["evidence_id"] for row in route_rows[-100:]],
                                    "Never promote a crypto entry without a current full-size exit route")
        return added

    def _learning(self, strategy: str, mechanism: str, status: str, statement: str,
                  sample: int, wins: int, expectancy: float | None,
                  stressed: float | None, evidence_ids: list[str], threshold: str) -> int:
        key = f"{strategy}:{mechanism}"
        payload = {"strategy": strategy, "mechanism": mechanism, "status": status,
                   "statement": statement, "sample_size": sample, "wins": wins,
                   "expectancy_usd": expectancy, "cost_stressed_expectancy_usd": stressed,
                   "evidence_ids": evidence_ids, "adoption_threshold": threshold}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        learning_id = hashlib.sha256(body.encode()).hexdigest()
        with self.lock, self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO learnings VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                learning_id, key, utcnow(), strategy, mechanism, status, statement,
                sample, wins, expectancy, stressed, json.dumps(evidence_ids), threshold, body))
        return int(cursor.rowcount == 1)

    def report(self, limit: int = 100) -> dict:
        with self.lock:
            totals = dict(self.db.execute("""SELECT COUNT(*) observations,
                COUNT(DISTINCT strategy) strategies,
                SUM(CASE WHEN cohort LIKE '%SHADOW%' THEN 1 ELSE 0 END) shadow_observations,
                SUM(CASE WHEN cohort='NEAR_MISS_SHADOW' THEN 1 ELSE 0 END) near_misses,
                SUM(CASE WHEN cohort='HARD_REJECT_CONTROL' THEN 1 ELSE 0 END) hard_reject_controls,
                (SELECT COUNT(*) FROM checkpoints) checkpoints,
                (SELECT COUNT(*) FROM learnings) learnings
                FROM observations""").fetchone())
            strategy_rows = [dict(row) for row in self.db.execute("""SELECT strategy,
                COUNT(*) observations,
                SUM(CASE WHEN realized_pnl_usd IS NOT NULL THEN 1 ELSE 0 END) closed,
                SUM(CASE WHEN realized_pnl_usd>0 THEN 1 ELSE 0 END) wins,
                ROUND(AVG(realized_pnl_usd),8) expectancy_usd,
                ROUND(AVG(COALESCE(cost_stressed_pnl_usd,realized_pnl_usd)),8) cost_stressed_expectancy_usd
                FROM observations GROUP BY strategy ORDER BY closed DESC,observations DESC""").fetchall()]
            learning_rows = [dict(row) for row in self.db.execute("""SELECT l.* FROM learnings l
                JOIN (SELECT learning_key,MAX(created_at) created_at FROM learnings GROUP BY learning_key) x
                ON x.learning_key=l.learning_key AND x.created_at=l.created_at
                ORDER BY l.created_at DESC LIMIT ?""", (limit,)).fetchall()]
            recent = [dict(row) for row in self.db.execute("""SELECT evidence_id,observed_at,
                source_service,asset_class,strategy,strategy_version,cohort,mode,instrument,
                event_type,decision,realized_pnl_usd,cost_stressed_pnl_usd,mfe_usd,mae_usd
                FROM observations ORDER BY recorded_at DESC LIMIT ?""", (limit,)).fetchall()]
            runs = [dict(row) for row in self.db.execute(
                "SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT 10").fetchall()]
        return {"generated_at": utcnow(), "database": "PERSISTENT_SQLITE_APPEND_ONLY",
                "checkpoints_minutes": list(CHECKPOINTS), "totals": totals,
                "strategies": strategy_rows, "learnings": learning_rows,
                "recent_evidence": recent, "recent_ingestion_runs": runs,
                "promotion_policy": {
                    "exit_rule": "30-50 independent cost-stressed closed observations",
                    "model_or_risk": "100 independent positive cost-stressed closed observations",
                    "automatic_promotion": False,
                }}
