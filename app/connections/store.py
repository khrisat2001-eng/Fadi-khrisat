"""SQLite persistence for connections, encrypted credentials, alerts and audit log."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from app.security.vault import EncryptedCredentials, b64, unb64

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    state TEXT NOT NULL,
    health TEXT NOT NULL DEFAULT 'unknown',
    trading_mode TEXT NOT NULL DEFAULT 'paper',
    key_hint TEXT,
    permissions TEXT NOT NULL DEFAULT '[]',
    raw_permissions TEXT NOT NULL DEFAULT '[]',
    ip_restricted INTEGER,
    account_id TEXT,
    issues TEXT NOT NULL DEFAULT '[]',
    balances TEXT NOT NULL DEFAULT '[]',
    available_pairs TEXT NOT NULL DEFAULT '[]',
    selected_pairs TEXT NOT NULL DEFAULT '[]',
    allocation_pct REAL,
    last_error TEXT,
    last_sync_at TEXT,
    last_test_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    connection_id TEXT PRIMARY KEY REFERENCES connections(id),
    key_id TEXT NOT NULL,
    wrapped_dek TEXT NOT NULL,
    nonce TEXT NOT NULL,
    ciphertext TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    at TEXT NOT NULL
);
"""

JSON_FIELDS = ("permissions", "raw_permissions", "issues", "balances", "available_pairs", "selected_pairs")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    # --- connections ---------------------------------------------------------
    def insert_connection(self, row: dict[str, Any]) -> None:
        row = {**row, "created_at": now_iso(), "updated_at": now_iso()}
        row = {k: json.dumps(v) if k in JSON_FIELDS else v for k, v in row.items()}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock, self._db:
            self._db.execute(f"INSERT INTO connections ({cols}) VALUES ({marks})", list(row.values()))

    def update_connection(self, connection_id: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()]
        with self._lock, self._db:
            self._db.execute(f"UPDATE connections SET {sets} WHERE id = ?", [*values, connection_id])

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
        return self._decode(r) if r else None

    def list_connections(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM connections ORDER BY created_at").fetchall()
        return [self._decode(r) for r in rows]

    @staticmethod
    def _decode(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for k in JSON_FIELDS:
            d[k] = json.loads(d[k])
        if d["ip_restricted"] is not None:
            d["ip_restricted"] = bool(d["ip_restricted"])
        return d

    # --- credentials ---------------------------------------------------------
    def put_credentials(self, connection_id: str, enc: EncryptedCredentials) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO credentials (connection_id, key_id, wrapped_dek, nonce, ciphertext) VALUES (?, ?, ?, ?, ?)",
                (connection_id, enc.key_id, b64(enc.wrapped_dek), b64(enc.nonce), b64(enc.ciphertext)),
            )

    def get_credentials(self, connection_id: str, key_hint: str) -> EncryptedCredentials | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM credentials WHERE connection_id = ?", (connection_id,)).fetchone()
        if not r:
            return None
        return EncryptedCredentials(
            key_id=r["key_id"],
            wrapped_dek=unb64(r["wrapped_dek"]),
            nonce=unb64(r["nonce"]),
            ciphertext=unb64(r["ciphertext"]),
            key_hint=key_hint,
        )

    def delete_credentials(self, connection_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM credentials WHERE connection_id = ?", (connection_id,))

    # --- alerts and audit ----------------------------------------------------
    def add_alert(self, connection_id: str | None, level: str, message: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO alerts (connection_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (connection_id, level, message, now_iso()),
            )

    def list_alerts(self, include_acknowledged: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM alerts" + ("" if include_acknowledged else " WHERE acknowledged = 0") + " ORDER BY id DESC LIMIT 200"
        with self._lock:
            return [dict(r) for r in self._db.execute(q).fetchall()]

    def acknowledge_alert(self, alert_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))

    def audit(self, connection_id: str | None, action: str, detail: str = "") -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO audit_log (connection_id, action, detail, at) VALUES (?, ?, ?, ?)",
                (connection_id, action, detail, now_iso()),
            )

    def list_audit(self, connection_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT action, detail, at FROM audit_log WHERE connection_id = ? ORDER BY id DESC LIMIT 100", (connection_id,)
            ).fetchall()
        return [dict(r) for r in rows]
