from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, data_dir: str, encryption_key: str):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(encryption_key.encode())
        self.db = sqlite3.connect(root / "coinbase_mcp.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, ticket_id TEXT, payload TEXT NOT NULL, digest TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS positions (ticket_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, order_id TEXT, status TEXT NOT NULL, entry_notional REAL NOT NULL, entry_price REAL NOT NULL, base_size REAL NOT NULL DEFAULT 0, entry_fees REAL NOT NULL DEFAULT 0, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT, exit_value REAL, exit_fees REAL NOT NULL DEFAULT 0, realized_pnl REAL)")
        self.db.commit()

    def save_credentials(self, key_name: str, private_key: str) -> None:
        for name, value in (("key_name", key_name), ("private_key", private_key)):
            encrypted = self.cipher.encrypt(value.encode())
            self.db.execute("INSERT INTO secrets(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, encrypted))
        self.db.commit()
        self.event("CREDENTIALS_REPLACED", {"key_fingerprint": hashlib.sha256(key_name.encode()).hexdigest()[:12]})

    def credentials(self) -> tuple[str, str]:
        rows = dict(self.db.execute("SELECT name,value FROM secrets").fetchall())
        if "key_name" not in rows or "private_key" not in rows:
            raise RuntimeError("Coinbase credentials not configured")
        return self.cipher.decrypt(rows["key_name"]).decode(), self.cipher.decrypt(rows["private_key"]).decode()

    def setting(self, name: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, name: str, value: object) -> None:
        self.db.execute("INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, str(value)))
        self.db.commit()

    def initialize_baseline(self, usdc_total: float) -> float:
        existing = self.setting("pilot_baseline_usdc")
        if existing is not None:
            return float(existing)
        if not 5 <= usdc_total <= 30:
            raise RuntimeError("Initial dedicated USDC balance must be between $5 and $30")
        self.set_setting("pilot_baseline_usdc", f"{usdc_total:.8f}")
        self.set_setting("realized_pnl_usdc", "0")
        self.event("PILOT_BASELINE_INITIALIZED", {"usdc": usdc_total})
        return usdc_total

    def permitted_capital(self) -> float:
        return max(0.0, float(self.setting("pilot_baseline_usdc", "0") or 0) + float(self.setting("realized_pnl_usdc", "0") or 0))

    def event(self, kind: str, payload: dict, ticket_id: str | None = None) -> None:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        self.db.execute("INSERT INTO events(at,kind,ticket_id,payload,digest) VALUES(?,?,?,?,?)", (datetime.now(timezone.utc).isoformat(), kind, ticket_id, raw, hashlib.sha256(raw.encode()).hexdigest()))
        self.db.commit()

    def seen(self, ticket_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM events WHERE ticket_id=? LIMIT 1", (ticket_id,)).fetchone() is not None

    def add_submitted_position(self, ticket: dict, order_id: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("INSERT INTO positions(ticket_id,product_id,order_id,status,entry_notional,entry_price,opened_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (ticket["ticket_id"], ticket["product_id"], order_id, "SUBMITTED", float(ticket["notional_usdc"]), float(ticket["limit_price"]), now, now))
        self.db.commit()

    def open_position(self) -> dict | None:
        row = self.db.execute("SELECT * FROM positions WHERE status NOT IN ('CLOSED','CANCELLED','FAILED') ORDER BY opened_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def update_position(self, ticket_id: str, **fields: object) -> None:
        allowed = {"order_id", "status", "base_size", "entry_fees", "updated_at", "closed_at", "exit_value", "exit_fees", "realized_pnl"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ",".join(f"{key}=?" for key in updates)
        self.db.execute(f"UPDATE positions SET {assignments} WHERE ticket_id=?", (*updates.values(), ticket_id))
        self.db.commit()

    def add_realized_pnl(self, pnl: float) -> None:
        total = float(self.setting("realized_pnl_usdc", "0") or 0) + pnl
        self.set_setting("realized_pnl_usdc", f"{total:.8f}")

    def paused(self) -> bool:
        row = self.db.execute("SELECT kind FROM events WHERE kind IN ('PAUSED','RESUMED') ORDER BY seq DESC LIMIT 1").fetchone()
        return bool(row and row[0] == "PAUSED")

    def recent(self, limit: int = 25) -> list[dict]:
        rows = self.db.execute("SELECT seq,at,kind,ticket_id,digest FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]
