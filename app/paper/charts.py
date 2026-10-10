"""Data for the exchange-style Trade screen: the market list (ranked by opportunity) and one pair's chart."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.exchanges.base import TIMEFRAME_SECONDS, ExchangeError
from app.strategy.indicators import ema

from .autopilot import plain_reason

if TYPE_CHECKING:
    from .engine import PaperTradingService

CHART_CANDLES = 100
FETCH_CANDLES = 150  # extra history so the moving averages are already settled at the chart's left edge


async def _candles(paper: "PaperTradingService", exchange: str, symbol: str):
    cfg = paper.strategy_config()
    try:
        return await paper.strategy.candles.get(exchange, symbol, cfg.timeframe, max(cfg.candles_needed, FETCH_CANDLES))
    except (ExchangeError, ValueError, OSError):
        return []


def _signal_summary(sig: dict[str, Any]) -> dict[str, Any]:
    reasons = sig.get("reasons") or []
    return {
        "status": sig["status"], "score": sig.get("score", 0),
        "suggested_stop": sig.get("suggested_stop"), "suggested_target": sig.get("suggested_target"),
        "checks": [{"ok": r.startswith("✓"), "text": plain_reason(r)} for r in reasons],
    }


async def markets_view(paper: "PaperTradingService", connection_id: str) -> dict[str, Any]:
    conn = paper._paper_connection(connection_id)
    tf = TIMEFRAME_SECONDS[paper.strategy_config().timeframe]
    rows = []
    for symbol in conn["selected_pairs"]:
        t = paper.market.latest(conn["exchange"], symbol)
        candles = await _candles(paper, conn["exchange"], symbol)
        sig = await paper._signal_for(conn, symbol)
        last = float(t.last) if t else (float(candles[-1].close) if candles else None)
        back = max(1, 86400 // tf)
        ref = float(candles[-back].open) if len(candles) >= back else (float(candles[0].open) if candles else None)
        rows.append({
            "symbol": symbol, "last": last, "bid": str(t.bid) if t else None, "ask": str(t.ask) if t else None,
            "change_24h_pct": round((last / ref - 1) * 100, 2) if last and ref else None,
            "holding": paper.trading.position(connection_id, symbol) is not None,
            **_signal_summary(sig),
        })
    rows.sort(key=lambda r: (r["status"] == "BUY_SETUP", r["score"]), reverse=True)
    best = next((r["symbol"] for r in rows if r["status"] == "BUY_SETUP" and not r["holding"]), None)
    return {"exchange": conn["exchange"], "timeframe": paper.strategy_config().timeframe, "best": best, "markets": rows}


async def chart_view(paper: "PaperTradingService", connection_id: str, symbol: str) -> dict[str, Any]:
    conn = paper._paper_connection(connection_id)
    cfg = paper.strategy_config()
    candles = await _candles(paper, conn["exchange"], symbol)
    closes = [float(c.close) for c in candles]
    fast = ema(closes, cfg.ema_fast) if closes else []
    slow = ema(closes, cfg.ema_slow) if closes else []
    keep = slice(-CHART_CANDLES, None)
    sig = await paper._signal_for(conn, symbol)
    pos = paper.trading.position(connection_id, symbol)
    first_ms = candles[-CHART_CANDLES:][0].open_time_ms if candles else 0
    markers = []
    for f in paper.trading.fills(connection_id, 200):
        if f["symbol"] != symbol:
            continue
        ms = int(datetime.fromisoformat(f["at"]).timestamp() * 1000)
        if ms >= first_ms:
            markers.append({"t": ms, "side": f["side"], "price": float(f["price"]), "reason": f["reason"]})
    m = sig.get("metrics") or {}
    t = paper.market.latest(conn["exchange"], symbol)
    return {
        "symbol": symbol, "exchange": conn["exchange"], "timeframe": cfg.timeframe,
        "ema_fast_period": cfg.ema_fast, "ema_slow_period": cfg.ema_slow,
        "candles": [[c.open_time_ms, float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume)]
                    for c in candles[keep]],
        "ema_fast": [None if x is None else round(x, 8) for x in fast[keep]],
        "ema_slow": [None if x is None else round(x, 8) for x in slow[keep]],
        "resistance": m.get("resistance"), "support": m.get("support"),
        "last": float(t.last) if t else None, "bid": str(t.bid) if t else None, "ask": str(t.ask) if t else None,
        "position": {"qty": str(pos["qty"]), "entry": float(pos["avg_price"]), "stop": float(pos["stop_price"]),
                     "target": float(pos["take_profit"]) if pos["take_profit"] is not None else None} if pos else None,
        "markers": markers,
        "signal": _signal_summary(sig),
    }
