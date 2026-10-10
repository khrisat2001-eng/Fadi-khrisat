"""The Home screen in one call: mode, accounts, positions, autopilot and a plain-language activity feed."""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .autopilot import Autopilot
    from .engine import PaperTradingService

EXIT_WORDS = {"stop_loss": "Stop-loss hit", "take_profit": "Take-profit hit", "manual_close": "Sold by you",
              "strategy_exit": "Autopilot sold: the trend weakened (candle closed below the fast average)"}


def fmt_num(x: Any, nd: int = 6) -> str:
    """Short, readable number: no trailing zeros, at most `nd` decimals."""
    d = Decimal(str(x))
    q = d.quantize(Decimal(1).scaleb(-nd)).normalize()
    return f"{q:f}"


def _buy_reason(decision: dict[str, Any] | None) -> str:
    if not decision:
        return ""
    sig = (decision.get("inputs") or {}).get("technical_signal") or {}
    m = sig.get("metrics") or {}
    parts = [] if sig.get("status") == "BUY_SETUP" else ["manual order without a strategy setup"]
    if sig.get("status") == "BUY_SETUP" and m.get("resistance") is not None:
        parts.append(f"breakout above {fmt_num(m['resistance'])}")
    if sig.get("status") == "BUY_SETUP" and m.get("rsi") is not None:
        parts.append(f"RSI {float(m['rsi']):.0f}")
    risk = ((decision.get("inputs") or {}).get("risk_config") or {}).get("max_risk_per_trade_pct")
    if risk is not None:
        parts.append(f"risk {risk}%")
    return ", ".join(parts)


def activity(paper: "PaperTradingService", connection_id: str, exchange: str, limit: int = 30) -> list[dict[str, Any]]:
    decisions = paper.trading.decisions(connection_id, 100)
    by_id = {d["id"]: d for d in decisions}
    items = []
    for f in paper.trading.fills(connection_id, limit):
        if f["side"] == "buy":
            d = by_id.get(f["decision_id"])
            who = "Autopilot" if d and d["inputs"].get("automatic") else "You"
            why = _buy_reason(d)
            items.append({"at": f["at"], "kind": "buy", "symbol": f["symbol"], "exchange": exchange,
                          "title": f"{who} bought {fmt_num(f['qty'])} {f['symbol']} at {fmt_num(f['price'])}",
                          "detail": why or "Strategy and safety checks passed.", "decision_id": f["decision_id"]})
        else:
            pnl = Decimal(f["realized_pnl"])
            items.append({"at": f["at"], "kind": "win" if pnl >= 0 else "loss", "symbol": f["symbol"], "exchange": exchange,
                          "title": f"Sold {fmt_num(f['qty'])} {f['symbol']} at {fmt_num(f['price'])}",
                          "detail": f"{EXIT_WORDS.get(f['reason'], f['reason'])}. Profit/loss {pnl:+.2f}."})
    for d in decisions:
        if d["status"] == "APPROVE":
            continue
        who = "Autopilot" if d["inputs"].get("automatic") else "Your order"
        items.append({"at": d["at"], "kind": "blocked", "symbol": d["symbol"], "exchange": exchange,
                      "title": f"{who} did not buy {d['symbol']}", "detail": d["summary"], "decision_id": d["id"]})
    items.sort(key=lambda x: x["at"], reverse=True)
    return items[:limit]


def home_view(paper: "PaperTradingService", autopilot: "Autopilot") -> dict[str, Any]:
    accounts = []
    feed: list[dict[str, Any]] = []
    for conn in paper.store.list_connections():
        if conn["state"] != "PAPER":
            continue
        view = paper.account_view(conn["id"])
        accounts.append({
            "connection_id": conn["id"], "exchange": conn["exchange"], "pairs": conn["selected_pairs"],
            "account": view["account"], "positions": view.get("positions", []),
            "autopilot": autopilot.status(conn["id"]),
            "market": paper.market.stream_state(conn["exchange"]),
        })
        if view["account"]:
            feed += activity(paper, conn["id"], conn["exchange"])
    feed.sort(key=lambda x: x["at"], reverse=True)
    connected = [c for c in paper.store.list_connections() if c["state"] in ("CONNECTED_READONLY", "PAPER")]
    return {
        "mode": "paper",
        "live_trading": "locked",
        "kill_switch": paper.kill_switch(),
        "connected_exchanges": [c["exchange"] for c in connected],
        "accounts": accounts,
        "activity": feed[:40],
    }
