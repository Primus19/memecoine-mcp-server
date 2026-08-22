from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, data_dir: str, encryption_key: str):
        root = Path(data_dir); root.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(encryption_key.encode())
        self.db = sqlite3.connect(root / "coinbase_mcp.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, ticket_id TEXT, payload TEXT NOT NULL, digest TEXT NOT NULL)")
        self.db.commit()

    def save_credentials(self, key_name: str, private_key: str) -> None:
        for name, value in (("key_name", key_name), ("private_key", private_key)):
            encrypted = self.cipher.encrypt(value.encode())
            self.db.execute("INSERT INTO secrets(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, encrypted))
        self.db.commit(); self.event("CREDENTIALS_REPLACED", {"key_fingerprint": hashlib.sha256(key_name.encode()).hexdigest()[:12]})

    def credentials(self) -> tuple[str, str]:
        rows = dict(self.db.execute("SELECT name,value FROM secrets").fetchall())
        if "key_name" not in rows or "private_key" not in rows: raise RuntimeError("Coinbase credentials not configured")
        return self.cipher.decrypt(rows["key_name"]).decode(), self.cipher.decrypt(rows["private_key"]).decode()

    def event(self, kind: str, payload: dict, ticket_id: str | None = None) -> None:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        self.db.execute("INSERT INTO events(at,kind,ticket_id,payload,digest) VALUES(?,?,?,?,?)", (datetime.now(timezone.utc).isoformat(), kind, ticket_id, raw, hashlib.sha256(raw.encode()).hexdigest()))
        self.db.commit()

    def seen(self, ticket_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM events WHERE ticket_id=? LIMIT 1", (ticket_id,)).fetchone() is not None

    def open_position(self) -> bool:
        buys = self.db.execute("SELECT count(*) FROM events WHERE kind='ORDER_SUBMITTED'").fetchone()[0]
        closes = self.db.execute("SELECT count(*) FROM events WHERE kind='POSITION_CLOSED'").fetchone()[0]
        return buys > closes

    def paused(self) -> bool:
        row = self.db.execute("SELECT kind FROM events WHERE kind IN ('PAUSED','RESUMED') ORDER BY seq DESC LIMIT 1").fetchone()
        return bool(row and row[0] == "PAUSED")

    def recent(self, limit: int = 25) -> list[dict]:
        rows = self.db.execute("SELECT seq,at,kind,ticket_id,digest FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("seq","at","kind","ticket_id","digest"), row)) for row in rows]

