"""Paper autopilot: places strategy entries on its own, in paper mode only.

When switched on for a paper connection, each run checks the strategy signal
for every selected pair. A BUY_SETUP on a new closed candle becomes an order
that goes through the same decision gate as a click on "Buy with strategy".
At most one attempt is made per pair per candle. For pairs it holds, it sells
when a closed candle after the entry finishes below the fast EMA, and raises
the stop as price rises (stops never move down). Stop-loss and take-profit are
also enforced on every price tick by the paper engine. It cannot touch real money.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.connections.service import PortalError
from app.connections.store import now_iso
from app.risk.gate import TradeRequest
from app.strategy.config import style_of

from .engine import PaperTradingService
from .home import fmt_num

if TYPE_CHECKING:
    from app.live.engine import LiveTradingService

log = logging.getLogger("app.autopilot")

STATE_KEY = "autopilot"
PLAIN_REASON = {  # condition: (when met, when not met)
    "Trend": ("The price is in an uptrend.", "The price is not in an uptrend."),
    "Breakout": ("The price broke above its recent high.", "The price has not broken above its recent high."),
    "Volume": ("Trading volume backs the move.", "Trading volume is too low to trust a breakout."),
    "Momentum": ("Momentum is in the buy range.", "Momentum is outside the buy range."),
    "Not over-extended": ("The price has not run too far.", "The price has already run too far."),
    "Over-extended": ("The price has not run too far.", "The price has already run too far, so it waits for a pullback."),
}


def plain_reason(reason: str) -> str:
    """'✗ Trend: close 100 vs EMA50 106' -> 'The price is not in an uptrend. (Trend: close 100 vs EMA50 106)'"""
    ok = reason.startswith("✓ ")
    text = reason[2:] if reason[:2] in ("✗ ", "✓ ") else reason
    key = text.split(":", 1)[0]
    if key not in PLAIN_REASON:
        return text
    plain = PLAIN_REASON[key][0 if ok else 1]
    return plain if key == text else f"{plain} ({text})"


class Autopilot:
    """One instance trades practice money; a second one, given `live`, trades the real-money book."""

    def __init__(self, paper: PaperTradingService, live: "LiveTradingService | None" = None):
        self.paper = paper
        self.live = live
        self.key = STATE_KEY if live is None else STATE_KEY + "_live"
        self.word = "paper" if live is None else "real money"
        # Last check per connection and pair, for the Home screen. Not persisted: rebuilt on the next run.
        self.last_checks: dict[str, dict[str, dict[str, Any]]] = {}
        self.last_run_at: str | None = None

    # --- on/off -------------------------------------------------------------------------
    def _state(self) -> dict[str, Any]:
        return self.paper.trading.get_setting(self.key) or {}

    def book(self, connection_id: str) -> str:
        """Whose money: the practice account, or the real-money book."""
        if self.live is None:
            return connection_id
        from app.live.engine import book_id
        return book_id(connection_id)

    def status(self, connection_id: str) -> dict[str, Any]:
        st = self._state().get(connection_id) or {}
        on = bool(st.get("on"))
        return {"on": on, "since": st.get("since"), "last_run_at": self.last_run_at,
                "style": style_of(self.paper.strategy_config()), "timeframe": self.paper.strategy_config().timeframe,
                "problems": self.problems(connection_id) if on else [],
                "pairs": list((self.last_checks.get(connection_id) or {}).values())}

    def problems(self, connection_id: str) -> list[str]:
        """Things that stop Auto-trade from buying at all, in plain words."""
        conn = self.paper.store.get_connection(connection_id)
        if conn is None:
            return ["This exchange connection no longer exists."]
        out = []
        ex = conn["exchange"].upper()
        if self.paper.kill_switch():
            out.append("Emergency stop is on, so nothing new is bought. Turn it off under Safety settings.")
        if conn["state"] != "PAPER":
            out.append(f"{ex} is not in paper-trading mode, so Auto-trade is not checking it.")
        if self.live is not None and not self.live.can_buy(connection_id):
            out.append("Real-money trading is locked for this exchange, so Auto-trade can only sell what it already holds.")
        elif self.paper.trading.get_account(self.book(connection_id)) is None:
            out.append("There is no practice account yet. Create one on the Trade tab.")
        if conn["health"] != "ok":
            out.append(f"The {ex} connection is not healthy ({conn['health']}). Check it on the Exchanges tab. "
                       "If the server runs in a US region, OKX and KuCoin refuse it: move the Railway region to EU West.")
        stream = self.paper.market.stream_state(conn["exchange"])
        if stream != "connected":
            out.append(f"Live prices from {ex} are not coming in (state: {stream}), and every buy needs a fresh price. "
                       "If the server runs in a US region, move the Railway region to EU West.")
        if self.last_run_at is None:
            out.append("Auto-trade checks have not run on this server yet.")
        elif (datetime.now(timezone.utc) - datetime.fromisoformat(self.last_run_at)).total_seconds() > 180:
            out.append(f"Auto-trade checks stopped running (last one at {self.last_run_at[:16].replace('T', ' ')} UTC). "
                       "Restart the app on Railway.")
        return out

    def set_on(self, connection_id: str, on: bool) -> dict[str, Any]:
        conn = self.paper._paper_connection(connection_id)
        if on and self.live is not None and not self.live.can_buy(connection_id):
            raise PortalError("Unlock real-money trading for this exchange first.", 409)
        if on and self.paper.trading.get_account(self.book(connection_id)) is None:
            raise PortalError("Create a paper account first.", 409)
        state = self._state()
        prev = state.get(connection_id) or {}
        state[connection_id] = {"on": on, "since": now_iso(), "done": prev.get("done", {})}
        self.paper.trading.set_setting(self.key, state)
        self.paper.store.audit(connection_id, ("autopilot_on" if on else "autopilot_off") + ("" if self.live is None else "_live"), "")
        self.paper.store.add_alert(connection_id, "critical" if on and self.live is not None else "info",
                                   f"Auto-trade turned {'ON' if on else 'OFF'} for {conn['exchange'].upper()} {self.word}.")
        if not on:
            self.last_checks.pop(connection_id, None)
        return self.status(connection_id)

    def _mark_done(self, connection_id: str, symbol: str, candle_time_ms: int | None) -> None:
        state = self._state()
        entry = state.setdefault(connection_id, {"on": True, "since": now_iso(), "done": {}})
        entry.setdefault("done", {})[symbol] = candle_time_ms
        self.paper.trading.set_setting(self.key, state)

    # --- the run ------------------------------------------------------------------------
    async def run_once(self) -> list[dict[str, Any]]:
        """Check every connection with autopilot on. Returns the orders it attempted."""
        self.last_run_at = now_iso()
        attempted = []
        for cid, st in self._state().items():
            if not st.get("on"):
                continue
            conn = self.paper.store.get_connection(cid)
            if conn is None or conn["state"] != "PAPER" or self.paper.trading.get_account(self.book(cid)) is None:
                continue
            attempted += await self._run_connection(conn, st.get("done", {}))
        return attempted

    async def _run_connection(self, conn: dict[str, Any], done: dict[str, Any]) -> list[dict[str, Any]]:
        cid = conn["id"]
        checks = self.last_checks.setdefault(cid, {})
        attempted = []
        if self.paper.kill_switch():
            for symbol in conn["selected_pairs"]:
                checks[symbol] = self._check(symbol, "paused", "Emergency stop is on, so no new trades.")
            return attempted
        for symbol in conn["selected_pairs"]:
            pos = self.paper.trading.position(self.book(cid), symbol)
            if pos is not None:
                checks[symbol] = await self._manage(conn, pos)
                continue
            if self.live is not None and not self.live.can_buy(cid):
                checks[symbol] = self._check(symbol, "paused", "Real-money trading is locked, so nothing new is bought.")
                continue
            try:
                sig = await self.paper._signal_for(conn, symbol)
            except Exception as exc:  # one pair failing must not stop the others
                log.warning("Autopilot signal error for %s: %s", symbol, type(exc).__name__)
                checks[symbol] = self._check(symbol, "error", "Couldn't get price candles right now. Will retry.")
                continue
            if sig["status"] != "BUY_SETUP":
                why = next((r for r in sig.get("reasons", []) if r.startswith("✗")), None) or \
                    (sig.get("reasons") or ["No buy setup."])[0]
                checks[symbol] = self._check(symbol, "waiting", f"No buy setup yet. {plain_reason(why)}")
                continue
            if done.get(symbol) == sig.get("candle_time_ms"):
                checks[symbol] = self._check(symbol, "waiting", "Already acted on this candle. Waiting for the next one.")
                continue
            result = await self._enter(conn, symbol)
            attempted.append(result)
            d = result["decision"]
            if d["status"] == "WAIT":
                # Something passing (a late price tick, a news delay): try again on the next run, same candle.
                checks[symbol] = self._check(symbol, "error", f"Buy setup found, but it has to wait: {d['summary']} "
                                             "Will try again in 30 seconds.")
                continue
            self._mark_done(cid, symbol, sig.get("candle_time_ms"))
            if result.get("fill"):
                f = result["fill"]
                checks[symbol] = self._check(symbol, "bought", f"Bought {fmt_num(f['qty'])} at {fmt_num(f['price'])}. It sells automatically below "
                                             f"{fmt_num(result['stop'])} (stop-loss) or above {fmt_num(result['target'])} (take-profit).")
            else:
                checks[symbol] = self._check(symbol, "blocked", f"Setup found but the safety checks said no. {d['summary']}")
        await self.paper.sync_streams()
        return attempted

    async def _manage(self, conn: dict[str, Any], pos: dict[str, Any]) -> dict[str, Any]:
        """For a held pair: sell on a trend break, otherwise raise the stop as price rises."""
        symbol, cfg = pos["symbol"], self.paper.strategy_config()
        holding = "Holding. It sells automatically at the stop-loss or take-profit"
        try:
            sig = await self.paper._signal_for(conn, symbol)
        except Exception as exc:  # keep the existing stop and target if analysis fails
            log.warning("Autopilot exit check error for %s: %s", symbol, type(exc).__name__)
            return self._check(symbol, "holding", holding + ".")
        m = sig.get("metrics") or {}
        if sig["status"] in ("STALE_DATA", "INSUFFICIENT_DATA") or "close" not in m:
            return self._check(symbol, "holding", holding + ". Chart data is not available right now.")
        candle_open = datetime.fromtimestamp(sig["candle_time_ms"] / 1000, timezone.utc)
        after_entry = candle_open >= datetime.fromisoformat(pos["opened_at"])
        if cfg.exit_on_trend_break and after_entry and m["close"] < m["ema_fast"]:
            why = f"a candle closed at {fmt_num(m['close'])}, below the fast average {fmt_num(m['ema_fast'])}."
            if await self._exit(conn["id"], symbol, "the trend weakened: " + why):
                return self._check(symbol, "sold", "Sold because the trend weakened: " + why)
            return self._check(symbol, "holding", "Wants to sell (trend weakened) but the sale has not gone through yet. Will retry.")
        if cfg.trailing_stop and m.get("atr"):
            close = Decimal(str(m["close"]))
            dist = max(Decimal(str(cfg.stop_atr)) * Decimal(str(m["atr"])), close * Decimal(str(cfg.min_stop_pct)) / 100)
            new_stop = close - dist
            new_stop = new_stop.quantize(Decimal("1e-8"))
            if new_stop > pos["stop_price"]:
                try:
                    self.paper.update_stop(conn["id"], symbol, new_stop, book=self.book(conn["id"]))
                    return self._check(symbol, "holding", f"Price rose, so the stop-loss moved up to {fmt_num(new_stop)} "
                                       "to protect profit. It never moves down.")
                except PortalError:
                    pass  # new stop at or above the bid: keep the current one
        target = f" or take-profit {fmt_num(pos['take_profit'])}" if pos["take_profit"] is not None else ""
        return self._check(symbol, "holding", f"{holding}: stop-loss {fmt_num(pos['stop_price'])}{target}. "
                           "It also sells if a candle closes below the fast average.")

    async def _enter(self, conn: dict[str, Any], symbol: str) -> dict[str, Any]:
        req = TradeRequest(conn["id"], symbol, "buy", None, None, None, source="strategy", automatic=True)
        try:
            result = await (self.paper.place_order(req) if self.live is None else self.live.place_order(req))
        except PortalError as exc:
            result = {"decision": {"status": "REJECT", "summary": exc.message}, "fill": None}
        result["stop"], result["target"] = req.stop_price, req.take_profit_price
        ex = conn["exchange"].upper()
        if result.get("fill") and self.live is not None:
            pass  # the real-money engine already raised its own REAL alert
        elif result.get("fill"):
            f = result["fill"]
            self.paper.store.add_alert(conn["id"], "info",
                                       f"Autopilot bought {fmt_num(f['qty'])} {symbol} on {ex} (paper) at {fmt_num(f['price'])}. "
                                       f"Stop {fmt_num(req.stop_price)}, target {fmt_num(req.take_profit_price)}.")
        elif result["decision"]["status"] != "WAIT":
            self.paper.store.add_alert(conn["id"], "info",
                                       f"Auto-trade ({self.word}) saw a buy setup on {ex} {symbol} but did not trade: "
                                       f"{result['decision']['summary']}")
        return result

    async def _exit(self, connection_id: str, symbol: str, why: str) -> bool:
        if self.live is None:
            return bool(self.paper.strategy_exit(connection_id, symbol, why))
        return await self.live.sell(connection_id, symbol, "strategy_exit", why) is not None

    @staticmethod
    def _check(symbol: str, state: str, text: str) -> dict[str, Any]:
        return {"symbol": symbol, "state": state, "text": text, "at": now_iso()}
