"""SQLite tables for paper trading, decisions, suspensions and risk settings.

Shares the connection and lock of the main Store. Money values are TEXT so
Decimals round-trip exactly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.connections.store import Store, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_accounts (
    connection_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    starting_balance TEXT NOT NULL,
    cash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty TEXT NOT NULL,
    avg_price TEXT NOT NULL,
    stop_price TEXT NOT NULL,
    take_profit TEXT,
    opened_at TEXT NOT NULL,
    decision_id TEXT,
    UNIQUE (connection_id, symbol)
);
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    reason TEXT NOT NULL,
    decision_id TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    checks TEXT NOT NULL,
    inputs TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suspensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,          -- global | exchange | asset
    target TEXT NOT NULL,         -- '*' | exchange id | asset code (e.g. BTC)
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lifted_at TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_day_start_iso() -> str:
    d = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.isoformat(timespec="seconds")


class TradingStore:
    def __init__(self, store: Store):
        self._db = store._db
        self._lock = store._lock
        with self._lock:
            self._db.executescript(SCHEMA)

    def _all(self, q: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args).fetchall()]

    def _one(self, q: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(q, args)
        return rows[0] if rows else None

    def _exec(self, q: str, args: tuple = ()) -> None:
        with self._lock, self._db:
            self._db.execute(q, args)

    # --- settings --------------------------------------------------------------
    def get_setting(self, key: str) -> Any | None:
        r = self._one("SELECT value FROM app_settings WHERE key = ?", (key,))
        return json.loads(r["value"]) if r else None

    def set_setting(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # --- accounts ----------------------------------------------------------------
    def get_account(self, connection_id: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM paper_accounts WHERE connection_id = ?", (connection_id,))
        if r:
            r["starting_balance"] = Decimal(r["starting_balance"])
            r["cash"] = Decimal(r["cash"])
        return r

    def create_account(self, connection_id: str, currency: str, balance: Decimal) -> None:
        self._exec(
            "INSERT INTO paper_accounts (connection_id, currency, starting_balance, cash, created_at) VALUES (?, ?, ?, ?, ?)",
            (connection_id, currency, str(balance), str(balance), now_iso()),
        )

    def delete_account(self, connection_id: str) -> None:
        with self._lock, self._db:
            for table in ("paper_accounts", "paper_positions"):
                self._db.execute(f"DELETE FROM {table} WHERE connection_id = ?", (connection_id,))

    # --- positions and fills -------------------------------------------------------
    @staticmethod
    def _pos(r: dict[str, Any]) -> dict[str, Any]:
        for k in ("qty", "avg_price", "stop_price"):
            r[k] = Decimal(r[k])
        r["take_profit"] = Decimal(r["take_profit"]) if r["take_profit"] else None
        return r

    def positions(self, connection_id: str | None = None) -> list[dict[str, Any]]:
        if connection_id is None:
            rows = self._all("SELECT * FROM paper_positions ORDER BY id")
        else:
            rows = self._all("SELECT * FROM paper_positions WHERE connection_id = ? ORDER BY id", (connection_id,))
        return [self._pos(r) for r in rows]

    def position(self, connection_id: str, symbol: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM paper_positions WHERE connection_id = ? AND symbol = ?", (connection_id, symbol))
        return self._pos(r) if r else None

    def apply_buy(self, connection_id: str, symbol: str, qty: Decimal, price: Decimal, fee: Decimal,
                  stop: Decimal, take_profit: Decimal | None, decision_id: str) -> None:
        """Atomically debit cash, open or add to the position and record the fill."""
        cost = qty * price + fee
        with self._lock, self._db:
            acct = self._db.execute("SELECT cash FROM paper_accounts WHERE connection_id = ?", (connection_id,)).fetchone()
            cash = Decimal(acct["cash"])
            if cost > cash:
                raise ValueError("Not enough paper cash for this order.")
            self._db.execute("UPDATE paper_accounts SET cash = ? WHERE connection_id = ?", (str(cash - cost), connection_id))
            existing = self._db.execute(
                "SELECT * FROM paper_positions WHERE connection_id = ? AND symbol = ?", (connection_id, symbol)
            ).fetchone()
            if existing:
                old_qty, old_avg = Decimal(existing["qty"]), Decimal(existing["avg_price"])
                new_qty = old_qty + qty
                new_avg = (old_qty * old_avg + qty * price) / new_qty
                # Adding never loosens protection: keep the tighter (higher) stop.
                new_stop = max(stop, Decimal(existing["stop_price"]))
                self._db.execute(
                    "UPDATE paper_positions SET qty = ?, avg_price = ?, stop_price = ?, take_profit = ? WHERE id = ?",
                    (str(new_qty), str(new_avg), str(new_stop), str(take_profit) if take_profit else existing["take_profit"], existing["id"]),
                )
            else:
                self._db.execute(
                    "INSERT INTO paper_positions (connection_id, symbol, qty, avg_price, stop_price, take_profit, opened_at, decision_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (connection_id, symbol, str(qty), str(price), str(stop), str(take_profit) if take_profit else None, now_iso(), decision_id),
                )
            self._db.execute(
                "INSERT INTO paper_fills (connection_id, symbol, side, qty, price, fee, realized_pnl, reason, decision_id, at) "
                "VALUES (?, ?, 'buy', ?, ?, ?, '0', 'entry', ?, ?)",
                (connection_id, symbol, str(qty), str(price), str(fee), decision_id, now_iso()),
            )

    def apply_close(self, connection_id: str, symbol: str, price: Decimal, fee_rate: Decimal, reason: str) -> dict[str, Any] | None:
        """Atomically close the whole position. Returns the fill, or None if already closed."""
        with self._lock, self._db:
            pos = self._db.execute(
                "SELECT * FROM paper_positions WHERE connection_id = ? AND symbol = ?", (connection_id, symbol)
            ).fetchone()
            if pos is None:
                return None
            qty, avg = Decimal(pos["qty"]), Decimal(pos["avg_price"])
            proceeds = qty * price
            fee = proceeds * fee_rate
            pnl = proceeds - fee - qty * avg
            acct = self._db.execute("SELECT cash FROM paper_accounts WHERE connection_id = ?", (connection_id,)).fetchone()
            self._db.execute(
                "UPDATE paper_accounts SET cash = ? WHERE connection_id = ?",
                (str(Decimal(acct["cash"]) + proceeds - fee), connection_id),
            )
            self._db.execute("DELETE FROM paper_positions WHERE id = ?", (pos["id"],))
            fill = {"connection_id": connection_id, "symbol": symbol, "side": "sell", "qty": str(qty), "price": str(price),
                    "fee": str(fee), "realized_pnl": str(pnl), "reason": reason, "at": now_iso()}
            self._db.execute(
                "INSERT INTO paper_fills (connection_id, symbol, side, qty, price, fee, realized_pnl, reason, decision_id, at) "
                "VALUES (:connection_id, :symbol, :side, :qty, :price, :fee, :realized_pnl, :reason, NULL, :at)",
                fill,
            )
            return fill

    def set_stop(self, position_id: int, stop: Decimal) -> None:
        self._exec("UPDATE paper_positions SET stop_price = ? WHERE id = ?", (str(stop), position_id))

    def fills(self, connection_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM paper_fills WHERE connection_id = ? ORDER BY id DESC LIMIT ?", (connection_id, limit))

    def realized_pnl_since(self, connection_id: str, since_iso: str) -> Decimal:
        rows = self._all("SELECT realized_pnl FROM paper_fills WHERE connection_id = ? AND at >= ?", (connection_id, since_iso))
        return sum((Decimal(r["realized_pnl"]) for r in rows), Decimal(0))

    # --- decisions -----------------------------------------------------------------
    def add_decision(self, d: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO decisions (id, connection_id, symbol, side, status, summary, checks, inputs, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d["id"], d["connection_id"], d["symbol"], d["side"], d["status"], d["summary"],
             json.dumps(d["checks"]), json.dumps(d["inputs"]), now_iso()),
        )

    def decisions(self, connection_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if connection_id:
            rows = self._all("SELECT * FROM decisions WHERE connection_id = ? ORDER BY at DESC, rowid DESC LIMIT ?", (connection_id, limit))
        else:
            rows = self._all("SELECT * FROM decisions ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            r["checks"] = json.loads(r["checks"])
            r["inputs"] = json.loads(r["inputs"])
        return rows

    # --- suspensions ------------------------------------------------------------------
    def add_suspension(self, scope: str, target: str, reason: str) -> None:
        self._exec("INSERT INTO suspensions (scope, target, reason, created_at) VALUES (?, ?, ?, ?)", (scope, target, reason, now_iso()))

    def lift_suspension(self, suspension_id: int) -> None:
        self._exec("UPDATE suspensions SET lifted_at = ? WHERE id = ? AND lifted_at IS NULL", (now_iso(), suspension_id))

    def active_suspensions(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM suspensions WHERE lifted_at IS NULL ORDER BY id")
