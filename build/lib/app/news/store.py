"""SQLite tables for the news engine. Shares the main Store's connection and lock.

Nothing here is ever created without a source row: every event links to the
news items it was built from, and every item keeps its URL, the source's
publication time and our own first-seen time.
"""
from __future__ import annotations

import json
from typing import Any

from app.connections.store import Store, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    tier INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    last_fetch_at TEXT,
    last_ok_at TEXT,
    last_error TEXT,
    items_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS news_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    severity TEXT NOT NULL,
    horizon TEXT NOT NULL,
    relevance REAL NOT NULL,
    volatility_risk REAL NOT NULL,
    confidence REAL NOT NULL,
    novelty REAL NOT NULL,
    confirmation TEXT NOT NULL,
    confirmation_reason TEXT NOT NULL,
    assets TEXT NOT NULL,          -- {"BTC": "name" | "ticker" | "model"}
    exchanges TEXT NOT NULL,       -- ["okx", ...]
    market_wide INTEGER NOT NULL,
    is_repeat_of INTEGER,
    first_seen_at TEXT NOT NULL,
    available_at TEXT NOT NULL,    -- earliest time any linked item was available to us
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    event_id INTEGER NOT NULL REFERENCES news_events(id),
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    headline TEXT NOT NULL,
    headline_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    available_at TEXT NOT NULL,    -- max(published_at, first_seen_at): what a point-in-time replay may see
    external_id TEXT,
    hedged INTEGER NOT NULL,
    denial INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS news_items_hash ON news_items(headline_hash);
CREATE INDEX IF NOT EXISTS news_items_event ON news_items(event_id);
CREATE TABLE IF NOT EXISTS news_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_item_id INTEGER NOT NULL REFERENCES news_items(id),
    event_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    severity TEXT NOT NULL,
    horizon TEXT NOT NULL,
    relevance REAL NOT NULL,
    volatility_risk REAL NOT NULL,
    confidence REAL NOT NULL,
    stated_facts TEXT NOT NULL,
    interpretation TEXT NOT NULL,
    model_assets TEXT NOT NULL,
    injection_flags TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_reactions (
    event_id INTEGER NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price_at_detection TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (event_id, exchange, symbol)
);
CREATE TABLE IF NOT EXISTS news_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,     -- UTC
    impact TEXT NOT NULL,           -- low | medium | high
    assets TEXT NOT NULL,           -- [] means market-wide
    source_url TEXT NOT NULL,
    status TEXT NOT NULL,           -- scheduled | confirmed | changed | completed | cancelled
    time_verified INTEGER NOT NULL,
    last_verified_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

_JSON = {"assets", "exchanges", "stated_facts", "model_assets", "injection_flags"}


def _decode(r: dict[str, Any]) -> dict[str, Any]:
    for k in _JSON & r.keys():
        r[k] = json.loads(r[k])
    return r


class NewsStore:
    def __init__(self, store: Store):
        self._db = store._db
        self._lock = store._lock
        with self._lock:
            self._db.executescript(SCHEMA)

    def _all(self, q: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_decode(dict(r)) for r in self._db.execute(q, args).fetchall()]

    def _one(self, q: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(q, args)
        return rows[0] if rows else None

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        row = {k: json.dumps(v) if k in _JSON else v for k, v in row.items()}
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        with self._lock, self._db:
            return self._db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row).lastrowid

    def _update(self, table: str, key: str, value: Any, **fields: Any) -> None:
        fields = {k: json.dumps(v) if k in _JSON else v for k, v in fields.items()}
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        with self._lock, self._db:
            self._db.execute(f"UPDATE {table} SET {sets} WHERE {key} = :_key", {**fields, "_key": value})

    # --- sources ---------------------------------------------------------------
    def seed_sources(self, sources: list[dict[str, Any]]) -> None:
        with self._lock, self._db:
            for s in sources:
                self._db.execute(
                    "INSERT OR IGNORE INTO news_sources (id, name, kind, url, tier, enabled, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], s["name"], s["kind"], s["url"], s["tier"], int(s["enabled"]), s.get("note", "")),
                )

    def sources(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM news_sources ORDER BY tier, name")
        for r in rows:
            r["enabled"] = bool(r["enabled"])
        return rows

    def source(self, source_id: str) -> dict[str, Any] | None:
        return next((s for s in self.sources() if s["id"] == source_id), None)

    def set_source_enabled(self, source_id: str, enabled: bool) -> None:
        self._update("news_sources", "id", source_id, enabled=int(enabled))

    def record_fetch(self, source_id: str, ok: bool, error: str | None, new_items: int = 0) -> None:
        now = now_iso()
        with self._lock, self._db:
            if ok:
                self._db.execute("UPDATE news_sources SET last_fetch_at = ?, last_ok_at = ?, last_error = NULL, "
                                 "items_seen = items_seen + ? WHERE id = ?", (now, now, new_items, source_id))
            else:
                self._db.execute("UPDATE news_sources SET last_fetch_at = ?, last_error = ? WHERE id = ?", (now, error, source_id))

    # --- items and events ----------------------------------------------------------
    def item_by_url(self, canonical: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM news_items WHERE canonical_url = ?", (canonical,))

    def items_by_hash(self, h: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM news_items WHERE headline_hash = ?", (h,))

    def add_item(self, row: dict[str, Any]) -> int:
        return self._insert("news_items", row)

    def event_items(self, event_id: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT i.*, s.name AS source_name, s.tier AS tier FROM news_items i JOIN news_sources s ON s.id = i.source_id "
            "WHERE i.event_id = ? ORDER BY i.available_at", (event_id,))

    def add_event(self, row: dict[str, Any]) -> int:
        return self._insert("news_events", row)

    def update_event(self, event_id: int, **fields: Any) -> None:
        self._update("news_events", "id", event_id, **fields)

    def event(self, event_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM news_events WHERE id = ?", (event_id,))

    def events_since(self, since_iso: str, until_iso: str | None = None) -> list[dict[str, Any]]:
        """Events available to us in [since, until]. `until` makes this point-in-time for replays."""
        if until_iso is None:
            return self._all("SELECT * FROM news_events WHERE available_at >= ? ORDER BY available_at DESC", (since_iso,))
        return self._all("SELECT * FROM news_events WHERE available_at >= ? AND available_at <= ? ORDER BY available_at DESC",
                         (since_iso, until_iso))

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM news_events ORDER BY available_at DESC, id DESC LIMIT ?", (limit,))

    def add_classification(self, row: dict[str, Any]) -> None:
        self._insert("news_classifications", {**row, "created_at": now_iso()})

    def classifications(self, event_id: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM news_classifications WHERE event_id = ? ORDER BY id", (event_id,))

    # --- reactions, actions -----------------------------------------------------------
    def add_reaction(self, event_id: int, exchange: str, symbol: str, price: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO news_reactions VALUES (?, ?, ?, ?, ?)", (event_id, exchange, symbol, price, now_iso()))

    def reactions(self, event_id: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM news_reactions WHERE event_id = ?", (event_id,))

    def add_action(self, event_id: int | None, action: str, detail: str) -> None:
        self._insert("news_actions", {"event_id": event_id, "action": action, "detail": detail, "at": now_iso()})

    def actions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM news_actions ORDER BY id DESC LIMIT ?", (limit,))

    def has_action(self, event_id: int, action: str) -> bool:
        return self._one("SELECT id FROM news_actions WHERE event_id = ? AND action = ?", (event_id, action)) is not None

    # --- calendar -----------------------------------------------------------------------
    def add_calendar(self, row: dict[str, Any]) -> int:
        return self._insert("calendar_events", {**row, "created_at": now_iso()})

    def update_calendar(self, cal_id: int, **fields: Any) -> None:
        self._update("calendar_events", "id", cal_id, **fields)

    def delete_calendar(self, cal_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM calendar_events WHERE id = ?", (cal_id,))

    def calendar(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM calendar_events ORDER BY scheduled_at")
