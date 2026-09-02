from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CHECKPOINTS = (5, 15, 30, 60, 120, 240)
CLOSE_EVENT_TYPES = (
    "TRADE_OR_DECISION", "TRADE_INTENT", "BROKER_ACTION", "BROKER_CLOSE", "PAPER_TRADE",
    "ARCHIVED_PAPER_TRADE", "PAPER_CLOSE", "PAPER_OR_LIVE_ACTION", "LIVE_PROBE",
)
HARD_RISK_TERMS = (
    "no sell route", "unsellable", "sell impact", "excessive impact",
    "sell simulation failed", "price impact above", "mint authority",
    "freeze authority", "unsafe", "stale", "honeypot",
    "concentration too high", "holder concentration above",
)
SOFT_RISK_TERMS = (
    "momentum", "acceleration", "retracement", "market cap", "volume",
    "buy pressure", "buyer", "age", "timing", "confirmation", "score",
    "concentration unavailable", "holder evidence unavailable",
    "creator evidence unavailable", "missing concentration",
)

RECOMMENDATION_PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def rule_family(rule: str) -> str:
    """Normalize threshold-specific rejection text into comparable mechanisms."""
    low = rule.lower()
    families = (
        ("sellability", ("sell route", "sell impact", "price impact", "sell simulation", "unsellable")),
        ("concentration_data", ("concentration", "holder evidence", "creator evidence", "top-holder")),
        ("liquidity", ("liquidity",)),
        ("market_cap", ("market cap", "capitalization")),
        ("momentum_acceleration", ("momentum", "acceleration")),
        ("volume", ("volume",)),
        ("buy_pressure", ("buy pressure", "buyer")),
        ("retracement", ("retracement",)),
        ("entry_quality", ("score", "confirmation", "timing")),
        ("age", ("age", "too old")),
    )
    for family, terms in families:
        if any(term in low for term in terms):
            return family
    return "other"


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

    Trade evidence is retained in full. High-frequency scan evidence is stored
    as a canonical summary plus a hash of the original payload so the database
    remains useful without duplicating entire discovery snapshots indefinitely.
    It never changes a strategy or authorizes a trade.
    """

    def __init__(self, path: str):
        self.path = path
        self.raw_retention_hours = max(
            6, int(os.getenv("TRADING_INTELLIGENCE_RAW_RETENTION_HOURS", "24")))
        self.hard_control_retention_days = max(
            2, int(os.getenv("TRADING_INTELLIGENCE_HARD_CONTROL_RETENTION_DAYS", "7")))
        self.ingestion_run_retention_days = max(
            1, int(os.getenv("TRADING_INTELLIGENCE_RUN_RETENTION_DAYS", "7")))
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
            CREATE TABLE IF NOT EXISTS maintenance_state(
              key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def compact(self, *, force: bool = False) -> dict:
        """Prune old, non-actionable raw noise without removing audit evidence.

        Confirmed trades, realized outcomes, executable checkpoints, learning
        references, and hard-risk controls are permanent.  Only old raw scan
        observations that have none of those properties are eligible.
        """
        now = datetime.now(timezone.utc)
        with self.lock:
            migration = self.db.execute(
                "SELECT value FROM maintenance_state WHERE key='storage_compaction_v2_applied'").fetchone()
            previous = self.db.execute(
                "SELECT value FROM maintenance_state WHERE key='last_compacted_at'").fetchone()
            if not force and migration and previous:
                try:
                    if now - datetime.fromisoformat(previous[0]) < timedelta(hours=6):
                        return {"status": "NOT_DUE", "last_compacted_at": previous[0]}
                except (TypeError, ValueError):
                    pass
            observation_cutoff = (now - timedelta(hours=self.raw_retention_hours)).isoformat()
            hard_cutoff = (now - timedelta(days=self.hard_control_retention_days)).isoformat()
            run_cutoff = (now - timedelta(days=self.ingestion_run_retention_days)).isoformat()
            before_bytes = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            with self.db:
                payloads_compacted = 0
                scan_types = ("CANDIDATE_DECISION", "SHADOW_FORWARD_PATH", "TRADE_CHECKPOINT",
                              "CANDIDATE_HANDOFF")
                marks = ",".join("?" for _ in scan_types)
                payload_rows = self.db.execute(f"""SELECT evidence_id,payload_json FROM observations
                    WHERE event_type IN ({marks}) AND payload_json NOT LIKE '%\"_storage_tier\":\"CANONICAL_SCAN_SUMMARY\"%'""",
                    scan_types).fetchall()
                for evidence_id, payload_json in payload_rows:
                    try:
                        item = json.loads(payload_json or "{}")
                    except (TypeError, json.JSONDecodeError):
                        item = {}
                    compacted = self.compact_scan_payload(item)
                    self.db.execute("UPDATE observations SET payload_json=? WHERE evidence_id=?",
                                    (json.dumps(compacted, sort_keys=True, separators=(",", ":")), evidence_id))
                    payloads_compacted += 1
                eligible = [row[0] for row in self.db.execute("""
                    SELECT o.evidence_id FROM observations o
                    WHERE o.recorded_at < ?
                      AND o.realized_pnl_usd IS NULL
                      AND o.event_type IN ('CANDIDATE_DECISION','SHADOW_FORWARD_PATH','TRADE_CHECKPOINT','CANDIDATE_HANDOFF')
                      AND o.cohort NOT IN ('PRODUCTION_QUALIFIED','PAPER_QUALIFIED')
                      AND (o.cohort!='HARD_REJECT_CONTROL' OR o.recorded_at < ?)
                      AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.evidence_id=o.evidence_id)
                      AND NOT EXISTS (
                        SELECT 1 FROM learnings l
                        WHERE instr(l.evidence_ids_json,o.evidence_id)>0)
                    """, (observation_cutoff, hard_cutoff)).fetchall()]
                if eligible:
                    marks = ",".join("?" for _ in eligible)
                    self.db.execute(f"DELETE FROM rule_evidence WHERE evidence_id IN ({marks})", eligible)
                    self.db.execute(f"DELETE FROM observations WHERE evidence_id IN ({marks})", eligible)
                run_cursor = self.db.execute("""DELETE FROM ingestion_runs
                    WHERE started_at < ? AND COALESCE(error,'')=''""", (run_cutoff,))
                self.db.execute("""INSERT INTO maintenance_state(key,value) VALUES('last_compacted_at',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (now.isoformat(),))
                self.db.execute("""INSERT INTO maintenance_state(key,value)
                    VALUES('storage_compaction_v2_applied',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (now.isoformat(),))
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # VACUUM is intentionally outside a transaction. It reclaims pages
            # after the evidence-safe deletes above and never runs per ingest.
            vacuum_error = ""
            try:
                self.db.execute("VACUUM")
            except sqlite3.OperationalError as exc:
                # At the 95% Railway alert there may not be enough temporary
                # space for VACUUM. Deleted pages remain reusable by SQLite, so
                # ingestion can continue without growing the file further.
                vacuum_error = str(exc)[:180]
            after_bytes = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"status": "COMPLETED", "completed_at": now.isoformat(),
                "raw_retention_hours": self.raw_retention_hours,
                "hard_control_retention_days": self.hard_control_retention_days,
                "payloads_compacted": payloads_compacted,
                "vacuum_error": vacuum_error,
                "observations_removed": len(eligible),
                "ingestion_runs_removed": max(0, run_cursor.rowcount),
                "bytes_before": before_bytes, "bytes_after": after_bytes,
                "bytes_reclaimed": max(0, before_bytes - after_bytes)}

    @staticmethod
    def compact_scan_payload(item: dict) -> dict:
        """Retain decision-grade scan facts without nested duplicate snapshots."""
        latest = item.get("latest_candidate") if isinstance(item.get("latest_candidate"), dict) else {}
        source = {**latest, **item}
        keys = (
            "id", "mint", "contract_address", "pool", "pool_address", "symbol", "instrument",
            "strategy", "strategy_version", "status", "decision", "cohort", "observed_at",
            "at", "first_seen_at", "last_seen_at", "source_url", "price_usd", "entry_price",
            "exit_price", "market_cap_usd", "liquidity_usd", "volume_24h_usd", "trades_5m",
            "unique_buyers_5m", "net_buy_pressure", "buyer_acceleration", "volume_acceleration",
            "price_change_5m_pct", "price_change_15m_pct", "return_since_seen",
            "retracement_from_high", "sell_simulation_ok", "sell_price_impact_bps",
            "top10_holder_fraction", "creator_fraction", "safety_evidence_status",
            "distribution_evidence_status", "paper_qualified", "qualified", "live_eligible",
            "paper_failures", "failures", "blocking_reasons", "rejection_reasons", "blockers",
            "entryUsd", "exitUsd", "realizedPnlUsd", "costStressedPnlUsd", "checkpoints",
        )
        retained = {key: source[key] for key in keys if source.get(key) is not None}
        retained["_storage_tier"] = "CANONICAL_SCAN_SUMMARY"
        return retained

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
        stored_item = (self.compact_scan_payload(item) if event_type in {
            "CANDIDATE_DECISION", "SHADOW_FORWARD_PATH", "TRADE_CHECKPOINT", "CANDIDATE_HANDOFF"
        } else item)
        stored_body = json.dumps(stored_item, sort_keys=True, separators=(",", ":"), default=str)
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
            stored_body, hashlib.sha256(body.encode()).hexdigest(),
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
        collections.append(("solana", "CRYPTO", "SOLANA_CANDIDATE_HANDOFF", "CANDIDATE_HANDOFF", "SHADOW",
                            list(solana.get("candidateHandoffs") or [])))
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
        maintenance = self.compact()
        return {"run_id": run_id, "observations_added": observations_added,
                "checkpoints_added": checkpoints_added, "learnings_added": learnings_added,
                "error": error, "maintenance": maintenance}

    def extract_learnings(self) -> int:
        close_types = ",".join("?" for _ in CLOSE_EVENT_TYPES)
        rows = [dict(row) for row in self.db.execute(f"""SELECT MIN(evidence_id) evidence_id,
            strategy,MIN(cohort) cohort,realized_pnl_usd,cost_stressed_pnl_usd
            FROM observations WHERE realized_pnl_usd IS NOT NULL AND event_type IN ({close_types})
            GROUP BY strategy,event_type,instrument,COALESCE(observed_at,''),realized_pnl_usd,
            COALESCE(cost_stressed_pnl_usd,realized_pnl_usd)""", CLOSE_EVENT_TYPES).fetchall()]
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
        givebacks = [dict(row) for row in self.db.execute("""SELECT evidence_id,strategy,
            realized_pnl_usd,mfe_usd FROM observations WHERE realized_pnl_usd IS NOT NULL
            AND mfe_usd>0 AND realized_pnl_usd<mfe_usd""").fetchall()]
        giveback_groups: dict[str, list[dict]] = defaultdict(list)
        for row in givebacks:
            giveback_groups[row["strategy"]].append(row)
        for strategy, values in giveback_groups.items():
            profit_to_loss = [row for row in values if float(row["realized_pnl_usd"]) <= 0]
            missed = [float(row["mfe_usd"]) - float(row["realized_pnl_usd"]) for row in values]
            statement = (f"{strategy} surrendered an average ${sum(missed)/len(missed):.4f} from MFE "
                         f"across {len(values)} closed trades; {len(profit_to_loss)} turned a positive "
                         "excursion into a non-profit. Test guarded profit retention prospectively.")
            added += self._learning(strategy, "PROFIT_GIVEBACK", "PROSPECTIVE_EXIT_EXPERIMENT",
                                    statement, len(values), len(values) - len(profit_to_loss),
                                    None, None, [row["evidence_id"] for row in values[-100:]],
                                    "30-50 independent closes with improved capture and no worse cost-stressed expectancy")
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

    @staticmethod
    def _recommendation_id(payload: dict) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def actionable_recommendations(self) -> list[dict]:
        """Convert retained evidence into guarded, testable actions.

        Recommendations are advisory and deterministic. They never mutate a
        strategy, authorize a trade, or bypass a hard execution/safety gate.
        """
        recommendations: list[dict] = []

        def add(*, priority: str, category: str, strategy: str, action: str,
                rationale: str, sample_size: int, confidence: str,
                evidence_ids: list[str], destinations: list[str], experiment: dict | None,
                adoption_threshold: str, live_change_allowed: bool = False) -> None:
            payload = {
                "priority": priority, "category": category, "strategy": strategy,
                "action": action, "rationale": rationale, "sample_size": sample_size,
                "confidence": confidence, "evidence_ids": evidence_ids[-25:],
                "eligible_destinations": destinations, "experiment": experiment,
                "adoption_threshold": adoption_threshold,
                "live_change_allowed": live_change_allowed,
            }
            payload["recommendation_id"] = self._recommendation_id(payload)
            recommendations.append(payload)

        close_types = ",".join("?" for _ in CLOSE_EVENT_TYPES)
        performance = [dict(row) for row in self.db.execute(f"""WITH independent_closes AS (
            SELECT MIN(evidence_id) evidence_id,strategy,
            COALESCE(cost_stressed_pnl_usd,realized_pnl_usd) pnl
            FROM observations WHERE realized_pnl_usd IS NOT NULL AND event_type IN ({close_types})
            GROUP BY strategy,event_type,instrument,COALESCE(observed_at,''),realized_pnl_usd,
            COALESCE(cost_stressed_pnl_usd,realized_pnl_usd))
            SELECT strategy,COUNT(*) sample_size,AVG(pnl) expectancy,
            GROUP_CONCAT(evidence_id) evidence_ids FROM independent_closes GROUP BY strategy""",
            CLOSE_EVENT_TYPES).fetchall()]
        for row in performance:
            sample, expectancy = int(row["sample_size"]), float(row["expectancy"] or 0)
            evidence_ids = str(row["evidence_ids"] or "").split(",")
            if sample < 100 or expectancy <= 0:
                add(priority="P0" if expectancy <= 0 else "P2", category="PROMOTION_BLOCKED",
                    strategy=row["strategy"],
                    action="Keep this cohort paper/shadow and preserve current risk limits.",
                    rationale=(f"Cost-stressed expectancy is ${expectancy:.4f} across {sample} closes; "
                               "the model/risk promotion gate is not met."),
                    sample_size=sample, confidence="HIGH" if sample >= 30 else "LOW_SAMPLE",
                    evidence_ids=evidence_ids, destinations=[row["strategy"]], experiment=None,
                    adoption_threshold="100 independent closes with positive cost-stressed expectancy")

        rules = [dict(row) for row in self.db.execute("""SELECT r.evidence_id,r.rule,r.severity,
            o.strategy,o.asset_class FROM rule_evidence r JOIN observations o
            ON o.evidence_id=r.evidence_id""").fetchall()]
        grouped_rules: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in rules:
            grouped_rules[(row["strategy"], rule_family(row["rule"]), row["severity"])].append(row)

        for (strategy, family, severity), values in grouped_rules.items():
            if family == "concentration_data" and len(values) >= 10:
                add(priority="P0", category="DATA_COLLECTION_FIX", strategy=strategy,
                    action="Repair holder/creator concentration enrichment and record source freshness.",
                    rationale=(f"Concentration evidence was unavailable in {len(values)} retained "
                               "decisions, preventing an auditable safety decision."),
                    sample_size=len(values), confidence="HIGH",
                    evidence_ids=[row["evidence_id"] for row in values], destinations=[strategy],
                    experiment={"metric": "concentration_coverage_rate", "target": ">=95% fresh coverage"},
                    adoption_threshold="95% fresh coverage over 100 consecutive candidates")
            if severity == "HARD" and family == "sellability" and values:
                add(priority="P0", category="KEEP_HARD_GATE", strategy=strategy,
                    action="Keep current full-size exit-route and impact checks mandatory.",
                    rationale=f"{len(values)} observations failed executable sellability.",
                    sample_size=len(values), confidence="HIGH",
                    evidence_ids=[row["evidence_id"] for row in values], destinations=[strategy],
                    experiment={"metric": "prevented_unsellable_entries", "execution": "forward-track only"},
                    adoption_threshold="Never relax from mark-price performance alone")

        handoff_failures = [dict(row) for row in self.db.execute("""SELECT evidence_id,strategy,
            instrument,decision FROM observations WHERE event_type='CANDIDATE_HANDOFF'
            AND decision='PAPER_ENTRY_QUOTE_FAILED'""").fetchall()]
        if handoff_failures:
            add(priority="P0", category="PIPELINE_RELIABILITY", strategy="SOLANA_CANDIDATE_HANDOFF",
                action="Investigate every qualified-candidate quote failure and retain the attempted executable entry.",
                rationale=(f"{len(handoff_failures)} qualified paper candidates reached the executor but "
                           "could not produce an executable entry quote."),
                sample_size=len(handoff_failures), confidence="HIGH",
                evidence_ids=[row["evidence_id"] for row in handoff_failures],
                destinations=["MICROCAP", "RUNNER", "DIVINE", "SOLANA_EARLY"],
                experiment={"metric": "qualified_to_paper_capture_rate", "target": ">=99%",
                            "failure_policy": "record and continue; never invent a fill"},
                adoption_threshold="99% handoff accounting over 100 qualified paper candidates")

        checkpoint_rows = [dict(row) for row in self.db.execute("""SELECT r.evidence_id,r.rule,
            o.strategy,o.asset_class,c.checkpoint_minutes,c.executable_pnl_usd,c.sell_route_ok
            FROM rule_evidence r JOIN observations o ON o.evidence_id=r.evidence_id
            JOIN checkpoints c ON c.evidence_id=r.evidence_id
            WHERE r.severity='SOFT' AND c.executable_pnl_usd IS NOT NULL""").fetchall()]
        horizon_rank = {60: 0, 30: 1, 15: 2, 120: 3, 240: 4, 5: 5}
        best: dict[tuple[str, str], dict] = {}
        for row in checkpoint_rows:
            key = (row["evidence_id"], rule_family(row["rule"]))
            if key not in best or horizon_rank.get(row["checkpoint_minutes"], 99) < horizon_rank.get(best[key]["checkpoint_minutes"], 99):
                best[key] = row
        outcomes: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in best.values():
            outcomes[(row["strategy"], rule_family(row["rule"]), row["asset_class"])].append(row)
        for (strategy, family, asset_class), values in outcomes.items():
            sample = len(values)
            stress = 0.02 if asset_class == "CRYPTO" else 0.01
            stressed = [float(row["executable_pnl_usd"]) - stress for row in values]
            expectancy = sum(stressed) / sample
            win_rate = sum(value > 0 for value in stressed) / sample
            ready = sample >= 10 and expectancy > 0 and win_rate >= .50
            add(priority="P1" if ready else "P3",
                category="SHADOW_EXPERIMENT" if ready else "COLLECT_MORE_EVIDENCE",
                strategy=strategy,
                action=(f"Prospectively shadow-test candidates failing only {family}; do not change live rules."
                        if ready else f"Continue executable forward tracking for the {family} rule family."),
                rationale=(f"One independent checkpoint per candidate gives n={sample}, "
                           f"cost-stressed expectancy ${expectancy:.4f}, win rate {win_rate:.1%}."),
                sample_size=sample, confidence="MEDIUM" if ready else "LOW_SAMPLE",
                evidence_ids=[row["evidence_id"] for row in values], destinations=[strategy],
                experiment={"rule_family": family, "mode": "SHADOW_ONLY", "cost_stress_usd": stress,
                            "primary_metric": "cost_stressed_expectancy_usd",
                            "control": "production-qualified and hard-rejected cohorts"},
                adoption_threshold="30-50 independent closes for exit rules; 100 positive cost-stressed closes for entry/model changes")

        givebacks = [dict(row) for row in self.db.execute("""SELECT learning_id,strategy,
            statement,sample_size,evidence_ids_json FROM learnings WHERE mechanism='PROFIT_GIVEBACK'
            ORDER BY created_at DESC""").fetchall()]
        latest_giveback: dict[str, dict] = {}
        for row in givebacks:
            latest_giveback.setdefault(row["strategy"], row)
        for strategy, row in latest_giveback.items():
            add(priority="P1", category="EXIT_EXPERIMENT", strategy=strategy,
                action="Shadow-test cost-aware profit retention after meaningful MFE and signal deterioration.",
                rationale=row["statement"], sample_size=int(row["sample_size"]),
                confidence="LOW_SAMPLE" if int(row["sample_size"]) < 30 else "MEDIUM",
                evidence_ids=json.loads(row["evidence_ids_json"]), destinations=[strategy],
                experiment={"mode": "SHADOW_ONLY", "trigger": [
                    "positive MFE", "alignment deterioration", "remaining edge below costs",
                    "configured MFE giveback"],
                    "compare": ["existing stop/target", "0.5R floor", "0.75R floor", "cost-aware deterioration exit"],
                    "metrics": ["profit_capture", "expectancy", "drawdown", "false_exit_rate"]},
                adoption_threshold="30-50 independent closes with better cost-stressed expectancy and no higher drawdown")

        recommendations.sort(key=lambda row: (RECOMMENDATION_PRIORITY.get(row["priority"], 9), row["strategy"], row["category"]))
        return recommendations

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
            close_types = ",".join("?" for _ in CLOSE_EVENT_TYPES)
            strategy_rows = [dict(row) for row in self.db.execute(f"""WITH evidence AS (
                SELECT strategy,COUNT(*) observations FROM observations GROUP BY strategy),
                independent_closes AS (SELECT strategy,realized_pnl_usd,
                COALESCE(cost_stressed_pnl_usd,realized_pnl_usd) stressed
                FROM observations WHERE realized_pnl_usd IS NOT NULL AND event_type IN ({close_types})
                GROUP BY strategy,event_type,instrument,COALESCE(observed_at,''),realized_pnl_usd,
                COALESCE(cost_stressed_pnl_usd,realized_pnl_usd)), stats AS (
                SELECT strategy,COUNT(*) closed,SUM(realized_pnl_usd>0) wins,
                ROUND(AVG(realized_pnl_usd),8) expectancy_usd,
                ROUND(AVG(stressed),8) cost_stressed_expectancy_usd
                FROM independent_closes GROUP BY strategy)
                SELECT e.strategy,e.observations,COALESCE(s.closed,0) closed,
                COALESCE(s.wins,0) wins,s.expectancy_usd,s.cost_stressed_expectancy_usd
                FROM evidence e LEFT JOIN stats s ON s.strategy=e.strategy
                ORDER BY closed DESC,observations DESC""", CLOSE_EVENT_TYPES).fetchall()]
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
        recommendations = self.actionable_recommendations()
        return {"generated_at": utcnow(), "database": "PERSISTENT_SQLITE_AUDIT_WITH_BOUNDED_RAW_RETENTION",
                "checkpoints_minutes": list(CHECKPOINTS), "totals": totals,
                "strategies": strategy_rows, "learnings": learning_rows,
                "recent_evidence": recent, "recent_ingestion_runs": runs,
                "actionable_recommendations": recommendations[:limit],
                "recommendation_summary": {
                    "total": len(recommendations),
                    "p0": sum(row["priority"] == "P0" for row in recommendations),
                    "p1": sum(row["priority"] == "P1" for row in recommendations),
                    "live_changes_authorized": sum(bool(row["live_change_allowed"]) for row in recommendations),
                },
                "promotion_policy": {
                    "exit_rule": "30-50 independent cost-stressed closed observations",
                    "model_or_risk": "100 independent positive cost-stressed closed observations",
                    "automatic_promotion": False,
                    "recommendations_are_advisory": True,
                    "hard_gates_never_relaxed_automatically": True,
                }, "storage_policy": {
                    "audit_evidence_permanent": True,
                    "trades_checkpoints_learnings_preserved": True,
                    "raw_non_actionable_retention_hours": self.raw_retention_hours,
                    "hard_control_retention_days": self.hard_control_retention_days,
                    "ingestion_run_retention_days": self.ingestion_run_retention_days,
                }}
