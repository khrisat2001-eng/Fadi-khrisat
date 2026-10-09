"""News pipeline: fetch -> normalise -> dedup/cluster -> attribute -> classify -> confirm
-> market reaction -> safety actions. Plus the gate's news assessment, the
calendar and the dashboard views.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from app.connections.store import Store, now_iso
from app.marketdata.supervisor import MarketDataManager
from app.paper.store import TradingStore

from .assets import AssetRegistry
from .classifier import Classifier, default_classifier
from .config import NewsConfig
from .models import CONFIRMED, CREDIBLE, SENTIMENT_SCORE, SEVERITIES, SUBCATEGORIES, RawItem
from .rules import INCIDENT_SUBS, NewsAssessment, assess, confirm, ts
from .sources import DEFAULT_SOURCES, PARSERS, Fetcher, http_fetch
from .store import NewsStore
from .text import canonical_url, headline_hash, is_denial, is_hedged, similarity

log = logging.getLogger("app.news")

MAX_ITEMS_PER_FETCH = 50
CAL_STATUSES = ("scheduled", "confirmed", "changed", "completed", "cancelled")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


class NewsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


class NewsService:
    def __init__(self, store: Store, trading: TradingStore, market: MarketDataManager,
                 classifier: Classifier | None = None, fetcher: Fetcher = http_fetch, registry: AssetRegistry | None = None):
        self.store = store
        self.trading = trading
        self.market = market
        self.news = NewsStore(store)
        self.news.seed_sources(DEFAULT_SOURCES)
        self.registry = registry or AssetRegistry()
        self.classifier = classifier or default_classifier(self.registry)
        self.fetcher = fetcher
        self._ingest_lock = asyncio.Lock()

    # --- settings ------------------------------------------------------------------
    def config(self) -> NewsConfig:
        return NewsConfig(**(self.trading.get_setting("news_config") or {}))

    def set_config(self, cfg: NewsConfig) -> NewsConfig:
        self.trading.set_setting("news_config", cfg.model_dump())
        self.store.audit(None, "news_config_changed", str(cfg.model_dump()))
        return cfg

    def set_source_enabled(self, source_id: str, enabled: bool) -> list[dict[str, Any]]:
        if self.news.source(source_id) is None:
            raise NewsError("Unknown news source.", 404)
        self.news.set_source_enabled(source_id, enabled)
        self.store.audit(None, "news_source_enabled" if enabled else "news_source_disabled", source_id)
        return self.news.sources()

    def sync_registry(self) -> None:
        for c in self.store.list_connections():
            for symbol in c["selected_pairs"]:
                self.registry.add_ticker(symbol.split("-")[0])

    # --- polling -------------------------------------------------------------------
    async def poll_once(self) -> dict[str, Any]:
        self.sync_registry()
        summary = {}
        for src in self.news.sources():
            if not src["enabled"]:
                continue
            try:
                raw = await self.fetcher(src)
                items = PARSERS[src["kind"]](src["id"], raw)[:MAX_ITEMS_PER_FETCH]
            except Exception as exc:  # one bad source must not stop the others
                err = f"{type(exc).__name__}: {str(exc)[:200]}"
                self.news.record_fetch(src["id"], False, err)
                summary[src["id"]] = {"ok": False, "error": err}
                log.warning("News source %s failed: %s", src["id"], type(exc).__name__)
                continue
            new = 0
            for item in items:
                if await self.ingest(item):
                    new += 1
            self.news.record_fetch(src["id"], True, None, new)
            summary[src["id"]] = {"ok": True, "new": new, "seen": len(items)}
        return summary

    # --- ingest -------------------------------------------------------------------
    async def ingest(self, raw: RawItem, now: datetime | None = None) -> dict[str, Any] | None:
        """Process one fetched item. Returns the event it joined, or None for an exact duplicate."""
        async with self._ingest_lock:
            return await self._ingest(raw, now or utcnow())

    async def _ingest(self, raw: RawItem, now: datetime) -> dict[str, Any] | None:
        src = self.news.source(raw.source_id)
        if src is None:
            raise NewsError("Items must come from an allowlisted source.")
        canon = canonical_url(raw.url)
        if self.news.item_by_url(canon):
            return None
        h = headline_hash(raw.headline)
        if any(i["source_id"] == raw.source_id for i in self.news.items_by_hash(h)):
            return None  # same headline re-published by the same source

        published = raw.published_at
        if published and published > now + timedelta(minutes=5):
            published = None  # a future publication time is not trusted
        available = max(published, now) if published else now
        text = f"{raw.headline}\n{raw.body}"
        attribution = self.registry.attribute(text)
        cls = await self.classifier.classify(raw.headline, raw.body, src["name"], src["tier"])
        for a in cls.model_assets:
            attribution.assets.setdefault(a, "model")  # shown, but never used to block

        event_id, is_new = self._cluster(raw.headline, cls.subcategory, attribution, now)
        if is_new:
            repeat_of = self._find_repeat(raw.headline, now)
            event_id = self.news.add_event({
                "title": raw.headline, "category": cls.category, "subcategory": cls.subcategory,
                "sentiment": cls.sentiment, "severity": cls.severity, "horizon": cls.horizon,
                "relevance": cls.relevance, "volatility_risk": cls.volatility_risk, "confidence": cls.confidence,
                "novelty": 0.2 if repeat_of else 1.0, "confirmation": "rumor", "confirmation_reason": "",
                "assets": attribution.assets, "exchanges": attribution.exchanges,
                "market_wide": int(cls.category == "C" or (cls.category == "B" and not attribution.assets)),
                "is_repeat_of": repeat_of, "first_seen_at": iso(now), "available_at": iso(available), "updated_at": iso(now),
            })
        item_id = self.news.add_item({
            "source_id": raw.source_id, "event_id": event_id, "url": raw.url, "canonical_url": canon,
            "headline": raw.headline, "headline_hash": h, "body": raw.body,
            "published_at": iso(published) if published else None, "first_seen_at": iso(now), "available_at": iso(available),
            "external_id": raw.external_id, "hedged": int(is_hedged(text)), "denial": int(is_denial(raw.headline)),
        })
        self.news.add_classification({
            "news_item_id": item_id, "event_id": event_id, "model_id": cls.model_id, "prompt_version": cls.prompt_version,
            "subcategory": cls.subcategory, "sentiment": cls.sentiment, "severity": cls.severity, "horizon": cls.horizon,
            "relevance": cls.relevance, "volatility_risk": cls.volatility_risk, "confidence": cls.confidence,
            "stated_facts": cls.stated_facts, "interpretation": cls.interpretation, "model_assets": cls.model_assets,
            "injection_flags": cls.injection_flags, "note": cls.note,
        })
        if cls.injection_flags:
            self.news.add_action(event_id, "manipulation_flag", f"Item {item_id} contains text aimed at automated readers; treated as tier 4.")
        event = self._recompute(event_id, attribution)
        if is_new:
            self._record_reaction(event)
        self._apply_safety(event)
        return event

    def _cluster(self, headline: str, sub: str, attribution, now: datetime) -> tuple[int | None, bool]:
        cfg = self.config()
        since = iso(now - timedelta(hours=cfg.cluster_window_hours))
        best, best_sim = None, 0.0
        for ev in self.news.events_since(since):
            sims = [similarity(headline, i["headline"]) for i in self.news.event_items(ev["id"])]
            sim = max(sims or [0.0])
            same_incident = (sub in INCIDENT_SUBS and ev["subcategory"] in INCIDENT_SUBS and (
                set(attribution.exchanges) & set(ev["exchanges"])
                or {a for a, how in attribution.assets.items() if how != "model"} & {a for a, how in ev["assets"].items() if how != "model"}))
            if same_incident:
                sim = max(sim, cfg.cluster_similarity)
            if sim >= cfg.cluster_similarity and sim > best_sim:
                best, best_sim = ev["id"], sim
        return (best, False) if best else (None, True)

    def _find_repeat(self, headline: str, now: datetime) -> int | None:
        cfg = self.config()
        older = self.news.events_since(iso(now - timedelta(days=cfg.repeat_lookback_days)), iso(now - timedelta(hours=cfg.cluster_window_hours)))
        for ev in older:
            if any(similarity(headline, i["headline"]) >= cfg.cluster_similarity for i in self.news.event_items(ev["id"])):
                return ev["is_repeat_of"] or ev["id"]
        return None

    def _recompute(self, event_id: int, attribution=None) -> dict[str, Any]:
        """Re-derive confirmation and the event's classification from all linked items."""
        ev = self.news.event(event_id)
        items = self.news.event_items(event_id)
        cls = {c["news_item_id"]: c for c in self.news.classifications(event_id)}
        rows = [{"tier": i["tier"], "source_id": i["source_id"], "hedged": bool(i["hedged"]), "denial": bool(i["denial"]),
                 "flagged": bool(cls[i["id"]]["injection_flags"]),
                 "direction": (SENTIMENT_SCORE[cls[i["id"]]["sentiment"]] > 0) - (SENTIMENT_SCORE[cls[i["id"]]["sentiment"]] < 0)}
                for i in items]
        status, reason = confirm(rows)
        claims = [i for i in items if not i["denial"]]
        lead_item = min(claims or items, key=lambda i: (4 if cls[i["id"]]["injection_flags"] else i["tier"], i["available_at"]))
        lead = cls[lead_item["id"]]
        severity = max((cls[i["id"]]["severity"] for i in claims or items), key=SEVERITIES.index)
        assets = dict(ev["assets"])
        if attribution is not None:
            for a, how in attribution.assets.items():
                if assets.get(a) in (None, "model"):
                    assets[a] = how
        exchanges = sorted(set(ev["exchanges"]) | set(attribution.exchanges if attribution else []))
        fields = {
            "subcategory": lead["subcategory"], "category": SUBCATEGORIES[lead["subcategory"]],
            "sentiment": "uncertain" if status == "disputed" else lead["sentiment"], "severity": severity,
            "horizon": lead["horizon"], "relevance": lead["relevance"], "volatility_risk": max(c["volatility_risk"] for c in cls.values()),
            "confidence": lead["confidence"] * (0.5 if status == "disputed" else 1.0),
            "confirmation": status, "confirmation_reason": reason, "assets": assets, "exchanges": exchanges,
            "available_at": min(i["available_at"] for i in items), "updated_at": now_iso(),
        }
        self.news.update_event(event_id, **fields)
        return self._enrich(self.news.event(event_id))

    def _enrich(self, ev: dict[str, Any]) -> dict[str, Any]:
        items = self.news.event_items(ev["id"])
        ev["items"] = items
        ev["best_tier"] = min((i["tier"] for i in items), default=4)
        ev["best_source"] = next((i["source_name"] for i in items if i["tier"] == ev["best_tier"]), None)
        return ev

    # --- market reaction -------------------------------------------------------------
    def _record_reaction(self, ev: dict[str, Any]) -> None:
        for asset, how in ev["assets"].items():
            if how == "model":
                continue
            for t in self.market.tickers():
                if t.symbol == f"{asset}-USDT":
                    self.news.add_reaction(ev["id"], t.exchange, t.symbol, str(t.mid))

    def reactions(self, event_id: int) -> list[dict[str, Any]]:
        out = []
        for r in self.news.reactions(event_id):
            t = self.market.latest(r["exchange"], r["symbol"])
            then = Decimal(r["price_at_detection"])
            change = float((t.mid - then) / then * 100) if t and then else None
            out.append({**r, "price_now": str(t.mid) if t else None,
                        "change_pct": round(change, 2) if change is not None else None})
        return out

    # --- safety actions ------------------------------------------------------------
    def _apply_safety(self, ev: dict[str, Any]) -> None:
        cfg = self.config()
        firm = ev["confirmation"] in (CONFIRMED, CREDIBLE)
        high = ev["severity"] in ("high", "critical")
        if ev.get("is_repeat_of"):
            return
        incident = high and ev["subcategory"] in INCIDENT_SUBS
        assets = [a for a, how in ev["assets"].items() if how != "model"]
        exchange_wide = ev["subcategory"] in ("security_incident", "exchange_incident") or not assets

        if incident and firm and cfg.auto_suspend_on_incidents and not self.news.has_action(ev["id"], "suspension"):
            targets = [("exchange", ex) for ex in ev["exchanges"]] if exchange_wide and ev["exchanges"] else [("asset", a) for a in assets]
            for scope, target in targets:
                reason = f"News: {ev['title'][:180]} ({ev['confirmation'].replace('_', ' ')}; event #{ev['id']})"
                self.trading.add_suspension(scope, target, reason)
                self.store.audit(None, "suspension_added", f"{scope} {target}: {reason}")
                self.store.add_alert(None, "critical", f"New entries suspended for {scope} {target.upper()} because of news: "
                                                        f"{ev['title'][:160]}. Open positions keep their stops. Lift the suspension "
                                                        f"in Risk controls once you've reviewed it.")
            if targets:
                self.news.add_action(ev["id"], "suspension", "; ".join(f"{s} {t}" for s, t in targets))

        # Open positions: tell the user when material news hits something they hold. Never sell automatically.
        if not (high and SENTIMENT_SCORE[ev["sentiment"]] <= 0):
            return
        for pos in self.trading.positions():
            conn = self.store.get_connection(pos["connection_id"])
            if conn is None:
                continue
            base = pos["symbol"].split("-")[0]
            hit = base in assets or (exchange_wide and conn["exchange"] in ev["exchanges"])
            key = f"position_alert:{pos['connection_id']}:{pos['symbol']}"
            if not hit or self.news.has_action(ev["id"], key):
                continue
            verified = "confirmed" if ev["confirmation"] == CONFIRMED else ev["confirmation"].replace("_", " ")
            self.store.add_alert(pos["connection_id"], "warning",
                                 f"News may change the risk of your paper {pos['symbol']} position on {conn['exchange'].upper()}: "
                                 f"{ev['title'][:160]} ({verified}). Its stop stays at {pos['stop_price']}; nothing is sold "
                                 f"automatically{'' if firm else ' on an unverified report'}. Review it.")
            self.news.add_action(ev["id"], key, f"Alerted on open position {pos['symbol']} (stop {pos['stop_price']}).")

    # --- gate assessment -------------------------------------------------------------
    def assess(self, exchange: str, symbol: str, *, technical_setup: bool | None, order_source: str,
               timeframe_seconds: int, spread_bps: float | None, now: datetime | None = None) -> NewsAssessment:
        now = now or utcnow()
        cfg = self.config()
        since = iso(now - timedelta(hours=cfg.max_news_age_hours))
        events = [self._enrich(e) for e in self.news.events_since(since, iso(now))]
        suspended = frozenset(a["event_id"] for a in self.news.actions(1000) if a["action"] == "suspension" and a["event_id"])
        return assess(events, self.calendar(now), self.news.sources(), cfg, asset=symbol.split("-")[0], exchange=exchange,
                      now=now, technical_setup=technical_setup, order_source=order_source,
                      timeframe_seconds=timeframe_seconds, spread_bps=spread_bps, suspended_event_ids=suspended)

    # --- calendar --------------------------------------------------------------------
    def calendar(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utcnow()
        stale = timedelta(days=self.config().calendar_stale_days)
        out = []
        for c in self.news.calendar():
            c["time_verified"] = bool(c["time_verified"])
            c["uncertain"] = not c["time_verified"] or now - ts(c["last_verified_at"]) > stale
            out.append(c)
        return out

    def add_calendar(self, name: str, scheduled_at: datetime, impact: str, assets: list[str], source_url: str,
                     time_verified: bool, status: str = "scheduled", notes: str = "") -> list[dict[str, Any]]:
        if urlsplit(source_url).scheme != "https" or not urlsplit(source_url).netloc:
            raise NewsError("Every calendar entry needs the https link of the source that states its date and time.")
        if scheduled_at.tzinfo is None:
            raise NewsError("Give the time with a timezone (it is stored in UTC).")
        self.news.add_calendar({
            "name": name, "scheduled_at": iso(scheduled_at), "impact": impact,
            "assets": sorted({a.strip().upper() for a in assets if a.strip()}), "source_url": source_url,
            "status": status, "time_verified": int(time_verified), "last_verified_at": now_iso(), "notes": notes,
        })
        self.store.audit(None, "calendar_added", f"{name} at {iso(scheduled_at)}")
        return self.calendar()

    def update_calendar(self, cal_id: int, status: str | None, time_verified: bool | None,
                        scheduled_at: datetime | None) -> list[dict[str, Any]]:
        if not any(c["id"] == cal_id for c in self.news.calendar()):
            raise NewsError("Calendar entry not found.", 404)
        fields: dict[str, Any] = {"last_verified_at": now_iso()}
        if status:
            fields["status"] = status
        if time_verified is not None:
            fields["time_verified"] = int(time_verified)
        if scheduled_at is not None:
            if scheduled_at.tzinfo is None:
                raise NewsError("Give the time with a timezone (it is stored in UTC).")
            fields["scheduled_at"] = iso(scheduled_at)
            fields.setdefault("status", "changed")
        self.news.update_calendar(cal_id, **fields)
        self.store.audit(None, "calendar_updated", f"{cal_id}: {fields}")
        return self.calendar()

    def delete_calendar(self, cal_id: int) -> list[dict[str, Any]]:
        self.news.delete_calendar(cal_id)
        self.store.audit(None, "calendar_deleted", str(cal_id))
        return self.calendar()

    # --- dashboard views ---------------------------------------------------------------
    def feed(self, asset: str | None = None, exchange: str | None = None, category: str | None = None,
             sentiment: str | None = None, confirmation: str | None = None, severity: str | None = None,
             source: str | None = None, since: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        out = []
        for ev in self.news.recent_events(500):
            ev = self._enrich(ev)
            if asset and asset.upper() not in ev["assets"]:
                continue
            if exchange and exchange.lower() not in ev["exchanges"]:
                continue
            if category and ev["category"] != category:
                continue
            if sentiment and ev["sentiment"] != sentiment:
                continue
            if confirmation and ev["confirmation"] != confirmation:
                continue
            if severity and ev["severity"] != severity:
                continue
            if source and not any(i["source_id"] == source for i in ev["items"]):
                continue
            if since and ev["available_at"] < since:
                continue
            ev["classifications"] = self.news.classifications(ev["id"])
            ev["reactions"] = self.reactions(ev["id"])
            for i in ev["items"]:
                i.pop("body", None)
                if i["published_at"]:
                    i["detect_latency_s"] = int((ts(i["first_seen_at"]) - ts(i["published_at"])).total_seconds())
            out.append(ev)
            if len(out) >= limit:
                break
        return out

    def overview(self) -> dict[str, Any]:
        """Per traded asset: current news assessment next to the technical status (filled in by the caller)."""
        return {"sources": self.news.sources(), "actions": self.news.actions(50), "config": self.config().model_dump()}

    def performance(self) -> dict[str, Any]:
        """Closed paper trades split by whether news was in play when the entry was approved."""
        decisions = {d["id"]: d for d in self.trading.decisions(limit=100000)}
        groups = {"technical_only": [], "news_in_play": []}
        for conn in self.store.list_connections():
            open_group: dict[str, str] = {}
            for f in sorted(self.trading.fills(conn["id"], 100000), key=lambda f: f["id"]):
                if f["side"] == "buy":
                    d = decisions.get(f["decision_id"] or "")
                    news = ((d or {}).get("inputs") or {}).get("news") or {}
                    open_group.setdefault(f["symbol"], "news_in_play" if news.get("events") else "technical_only")
                elif f["symbol"] in open_group:
                    groups[open_group.pop(f["symbol"])].append(Decimal(f["realized_pnl"]))
        out = {}
        for k, pnls in groups.items():
            wins = [p for p in pnls if p > 0]
            out[k] = {"trades": len(pnls), "wins": len(wins), "net_pnl": f"{sum(pnls, Decimal(0)):.2f}",
                      "avg_pnl": f"{(sum(pnls, Decimal(0)) / len(pnls)):.2f}" if pnls else None}
        out["note"] = ("Paper results after fees and slippage. News runs in safety-only mode, so it can only block or "
                       "shrink trades; small samples say little.")
        return out
