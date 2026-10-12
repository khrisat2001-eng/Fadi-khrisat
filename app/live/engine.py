"""Real mode: real spot orders on the exchange, inside a budget the owner sets.

Safety rules, in the order they apply:
  * The server switch LIVE_TRADING=on must be set on the server. The web page alone can never turn it on.
  * Each exchange is unlocked separately by typing the confirmation phrase. The key must allow trading;
    keys that can withdraw or transfer are still refused when they connect.
  * Every buy goes through the same decision gate as practice trades, sized from the real-money budget
    (not the whole account), then capped at the per-trade limit and the USDT really available.
  * Orders are never resent blindly. If the answer is lost, the order is looked up by its client id.
  * Stop-loss and take-profit are watched on every price tick, like practice trades, and sold at market.
    They live in this app, not on the exchange, so they only work while the server is running.

Real trades are kept in their own book ("<connection id>#live") in the same tables as practice trades,
so practice and real results never mix.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

from app.connections.service import PortalError
from app.connections.store import now_iso
from app.exchanges.base import ErrorKind, ExchangeError, OrderResult
from app.marketdata.models import Ticker
from app.paper.home import fmt_num
from app.risk.gate import PAPER_QUOTE, TradeRequest

if TYPE_CHECKING:
    from app.paper.engine import PaperTradingService

log = logging.getLogger("app.live")

STATE_KEY = "live"
CONFIRM_PHRASE = "REAL MONEY"
DEFAULT_PER_TRADE = Decimal("20")
DEFAULT_BUDGET = Decimal("100")
SELL_RETRY_SECONDS = 30
FILL_POLLS = 10


def book_id(connection_id: str) -> str:
    return f"{connection_id}#live"


def _floor(qty: Decimal, step: Decimal | None) -> Decimal:
    if not step or step <= 0:
        return qty.quantize(Decimal("1e-8"), rounding=ROUND_DOWN)
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


class LiveTradingService:
    def __init__(self, paper: "PaperTradingService", enabled: bool):
        self.paper = paper
        self.enabled = enabled
        self.trading = paper.trading
        self.store = paper.store
        self.connections = paper.connections
        self._selling: set[str] = set()
        self._sell_after: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None  # the server's loop, remembered on the first request
        paper.market.add_listener(self.on_ticker)

    # --- state ------------------------------------------------------------------------
    def _all_state(self) -> dict[str, Any]:
        return self.trading.get_setting(STATE_KEY) or {}

    def state(self, connection_id: str) -> dict[str, Any]:
        st = self._all_state().get(connection_id) or {}
        return {"unlocked": bool(st.get("unlocked")), "since": st.get("since"),
                "per_trade": Decimal(st.get("per_trade", DEFAULT_PER_TRADE)), "budget": Decimal(st.get("budget", DEFAULT_BUDGET))}

    def _save(self, connection_id: str, **fields: Any) -> None:
        state = self._all_state()
        cur = state.get(connection_id) or {"per_trade": str(DEFAULT_PER_TRADE), "budget": str(DEFAULT_BUDGET)}
        cur.update({k: str(v) if isinstance(v, Decimal) else v for k, v in fields.items()})
        state[connection_id] = cur
        self.trading.set_setting(STATE_KEY, state)

    def can_buy(self, connection_id: str) -> bool:
        return self.enabled and self.state(connection_id)["unlocked"]

    def unlocked_exchanges(self) -> list[str]:
        out = []
        for cid, st in self._all_state().items():
            conn = self.store.get_connection(cid)
            if self.enabled and st.get("unlocked") and conn:
                out.append(conn["exchange"])
        return out

    def requirements(self, conn: dict[str, Any]) -> list[dict[str, Any]]:
        ex = conn["exchange"].upper()
        trade_word = "Trade" if conn["exchange"] == "okx" else "Spot trading"
        return [
            {"ok": self.enabled, "text": "The server allows real money: the variable LIVE_TRADING is set to on in Railway Variables."},
            {"ok": conn["state"] == "PAPER" and conn["health"] == "ok",
             "text": f"{ex} is connected, healthy and in practice mode with your pairs chosen."},
            {"ok": "trade" in (conn["permissions"] or []),
             "text": f"Your {ex} API key has {trade_word} permission (and still no Withdraw or Transfer). "
                     "If not, create a new key with it and use Replace key on the Exchanges tab."},
        ]

    # --- unlock, lock, limits ---------------------------------------------------------
    async def unlock(self, connection_id: str, confirm: str) -> dict[str, Any]:
        self._loop = asyncio.get_running_loop()
        conn = self.paper._paper_connection(connection_id)
        if not self.enabled:
            raise PortalError("Real-money trading is switched off on this server. In Railway, open the service's Variables, "
                              "add LIVE_TRADING with the value on, and let it redeploy. Then unlock again.", 403)
        if (confirm or "").strip().upper() != CONFIRM_PHRASE:
            raise PortalError(f'Type {CONFIRM_PHRASE} to confirm you accept the risk of losing real money.')
        missing = [r["text"] for r in self.requirements(conn) if not r["ok"]]
        if missing:
            raise PortalError("Not ready for real money yet: " + " ".join(missing), 409)
        # Fresh lot sizes and balances straight from the exchange before the first real order.
        connector = self._connector(conn)
        try:
            pairs = await connector.get_trading_pairs()
            balances = await connector.get_balances()
        except ExchangeError as exc:
            raise PortalError(f"Couldn't reach {conn['exchange'].upper()} to check your account: {exc.plain_message}", 502) from None
        finally:
            await connector.aclose()
        self.store.update_connection(connection_id, available_pairs=[p.__dict__ for p in pairs],
                                     balances=[b.__dict__ for b in balances], last_sync_at=now_iso())
        st = self.state(connection_id)
        if self.trading.get_account(book_id(connection_id)) is None:
            self.trading.create_account(book_id(connection_id), PAPER_QUOTE, st["budget"])
        self._save(connection_id, unlocked=True, since=now_iso(), per_trade=st["per_trade"], budget=st["budget"])
        self.store.audit(connection_id, "live_unlocked", f"per_trade={st['per_trade']} budget={st['budget']}")
        self.store.add_alert(connection_id, "critical",
                             f"REAL-MONEY trading unlocked on {conn['exchange'].upper()}. Limits: {fmt_num(st['per_trade'])} {PAPER_QUOTE} "
                             f"per trade, {fmt_num(st['budget'])} {PAPER_QUOTE} in total.")
        return self.view(connection_id)

    def lock(self, connection_id: str) -> dict[str, Any]:
        conn = self.store.get_connection(connection_id)
        if conn is None:
            raise PortalError("Connection not found.", 404)
        self._save(connection_id, unlocked=False)
        self.store.audit(connection_id, "live_locked", "")
        self.store.add_alert(connection_id, "info", f"Real-money trading locked on {conn['exchange'].upper()}. No new real buys; "
                             "open real trades still sell at their stop-loss or take-profit.")
        return self.view(connection_id)

    def set_limits(self, connection_id: str, per_trade: Decimal, budget: Decimal) -> dict[str, Any]:
        if self.store.get_connection(connection_id) is None:
            raise PortalError("Connection not found.", 404)
        if per_trade < 5 or budget < per_trade:
            raise PortalError(f"The per-trade limit must be at least 5 {PAPER_QUOTE}, and the total budget at least the per-trade limit.")
        old = self.state(connection_id)["budget"]
        book = book_id(connection_id)
        if self.trading.get_account(book) is not None and budget != old:
            try:
                self.trading.adjust_account(book, budget - old)
            except ValueError:
                raise PortalError("The budget can't go below the money already in open real trades.") from None
        self._save(connection_id, per_trade=per_trade, budget=budget)
        self.store.audit(connection_id, "live_limits_changed", f"per_trade={per_trade} budget={budget}")
        return self.view(connection_id)

    # --- views ------------------------------------------------------------------------
    def view(self, connection_id: str) -> dict[str, Any]:
        conn = self.store.get_connection(connection_id)
        if conn is None:
            raise PortalError("Connection not found.", 404)
        st = self.state(connection_id)
        usdt = next((b for b in conn["balances"] if b["currency"] == PAPER_QUOTE), None)
        return {
            "connection_id": connection_id, "exchange": conn["exchange"],
            "server_switch": self.enabled, "unlocked": self.enabled and st["unlocked"], "since": st["since"],
            "per_trade": str(st["per_trade"]), "budget": str(st["budget"]), "confirm_phrase": CONFIRM_PHRASE,
            "real_usdt_available": usdt["available"] if usdt else None, "balances_at": conn["last_sync_at"],
            "requirements": self.requirements(conn),
            **{k: v for k, v in self.paper.account_view(connection_id, book_id(connection_id)).items() if k != "connection_id"},
        }

    # --- exchange helpers -------------------------------------------------------------
    def _connector(self, conn: dict[str, Any]):
        return self.connections.factory(conn["exchange"], self.connections._credentials(conn))

    @staticmethod
    def _pair(conn: dict[str, Any], symbol: str) -> dict[str, Any]:
        return next((p for p in conn["available_pairs"] if p["symbol"] == symbol), {})

    async def _send(self, connector, symbol: str, side: str, qty: Decimal) -> OrderResult:
        """Place a market order and wait for its fill. Never sends the same order twice."""
        client_id = "fk" + uuid.uuid4().hex[:24]
        order_id = None
        try:
            order_id = await connector.place_market_order(symbol, side, qty, client_id)
        except ExchangeError as exc:
            # A clear refusal (it has the exchange's own error code) means nothing was placed.
            if exc.exchange_code or exc.kind not in (ErrorKind.NETWORK, ErrorKind.EXCHANGE_UNAVAILABLE, ErrorKind.UNEXPECTED):
                raise
            await asyncio.sleep(1)
            found = await connector.get_order(symbol, client_id=client_id)
            if found is None:
                raise
            order_id = found.order_id
        result = None
        for _ in range(FILL_POLLS):
            result = await connector.get_order(symbol, order_id=order_id)
            if result is not None and result.status != "open":
                break
            await asyncio.sleep(0.3)
        if result is None:
            raise PortalError(f"The exchange accepted order {order_id} but doesn't show it yet. Check it on the exchange.", 502)
        return result

    async def _refresh_balances(self, conn: dict[str, Any], connector) -> list:
        balances = await connector.get_balances()
        self.store.update_connection(conn["id"], balances=[b.__dict__ for b in balances], last_sync_at=now_iso())
        return balances

    # --- buying -----------------------------------------------------------------------
    async def place_order(self, req: TradeRequest) -> dict[str, Any]:
        self._loop = asyncio.get_running_loop()
        cid = req.connection_id
        conn = self.paper._paper_connection(cid)
        if not self.enabled:
            raise PortalError("Real-money trading is switched off on this server (LIVE_TRADING is not on).", 403)
        st = self.state(cid)
        if not st["unlocked"]:
            raise PortalError("Real-money trading is locked for this exchange. Unlock it first.", 403)
        if req.side != "buy":
            raise PortalError("Use Sell on an open position to sell.")
        book = book_id(cid)
        cfg = self.paper.risk_config()

        connector = self._connector(conn)
        try:
            try:
                balances = await self._refresh_balances(conn, connector)
            except ExchangeError as exc:
                raise PortalError(f"Couldn't read your {conn['exchange'].upper()} balance: {exc.plain_message}", 502) from None
            real_usdt = next((Decimal(b.available) for b in balances if b.currency == PAPER_QUOTE), Decimal(0))

            ctx = await self.paper.build_context(conn, req.symbol, req.source, book=book)
            if ctx.account is not None:
                ctx.account = {**ctx.account, "cash": min(ctx.account["cash"], real_usdt)}
            if req.source == "strategy" and ctx.signal:
                if req.stop_price is None and ctx.signal.get("suggested_stop"):
                    req.stop_price = Decimal(str(ctx.signal["suggested_stop"]))
                if req.take_profit_price is None and ctx.signal.get("suggested_target"):
                    req.take_profit_price = Decimal(str(ctx.signal["suggested_target"]))
            if req.stop_price is None or req.take_profit_price is None:
                raise PortalError("A stop-loss and take-profit are required.")

            gate_req = TradeRequest(book, req.symbol, "buy", req.stop_price, req.take_profit_price, req.risk_pct, req.source, req.automatic)
            decision = self.paper.gate.evaluate(gate_req, ctx, cfg)
            d = decision.as_dict()
            d["inputs"] = {**d["inputs"], "mode": "real", "real_usdt_available": str(real_usdt), "per_trade_limit": str(st["per_trade"])}
            self.trading.add_decision({**d, "connection_id": book, "symbol": req.symbol, "side": "buy"})
            result: dict[str, Any] = {"decision": d, "fill": None, "mode": "real"}
            if decision.status != "APPROVE":
                return result

            # The token must match what the gate approved; the order can only be smaller than that.
            decision_id = self.paper.gate.verify(decision.token, book, req.symbol, "buy", decision.qty)
            pair = self._pair(conn, req.symbol)
            ask = ctx.ticker.ask
            qty = min(decision.qty, st["per_trade"] / ask)
            qty = _floor(qty, Decimal(pair["lot_size"]) if pair.get("lot_size") else None)
            if qty <= 0 or qty * ask < cfg.min_order_value or (pair.get("min_size") and qty < Decimal(pair["min_size"])):
                result["decision"] = {**d, "status": "REJECT",
                                      "summary": f"Order would be too small after the {fmt_num(st['per_trade'])} {PAPER_QUOTE} per-trade limit "
                                                 "and the exchange's size rules."}
                return result

            try:
                fill = await self._send(connector, req.symbol, "buy", qty)
            except ExchangeError as exc:
                self.store.add_alert(cid, "warning", f"Real buy of {req.symbol} on {conn['exchange'].upper()} failed: {exc.plain_message}")
                raise PortalError(f"The exchange refused the order: {exc.plain_message}", 502) from None
            if fill.filled_qty <= 0 or fill.avg_price is None:
                raise PortalError(f"Order {fill.order_id} was not filled ({fill.status}). Nothing was bought.", 502)
            self.trading.apply_buy(book, req.symbol, fill.filled_qty, fill.avg_price, fill.fee_quote,
                                   req.stop_price, req.take_profit_price, decision_id, strict=False)
            self.store.audit(cid, "live_buy", f"{fill.filled_qty} {req.symbol} @ {fill.avg_price} order {fill.order_id} decision {decision_id}")
            who = "Auto-trade" if req.automatic else "You"
            self.store.add_alert(cid, "info", f"REAL: {who} bought {fmt_num(fill.filled_qty)} {req.symbol} on {conn['exchange'].upper()} at "
                                 f"{fmt_num(fill.avg_price)}. Stop {fmt_num(req.stop_price)}, target {fmt_num(req.take_profit_price)}.")
            try:
                await self._refresh_balances(conn, connector)
            except ExchangeError:
                pass
            result["fill"] = {"side": "buy", "symbol": req.symbol, "qty": str(fill.filled_qty), "price": str(fill.avg_price),
                              "fee": str(fill.fee_quote), "order_id": fill.order_id}
            return result
        finally:
            await connector.aclose()

    # --- selling ----------------------------------------------------------------------
    async def sell(self, connection_id: str, symbol: str, reason: str, why: str = "") -> dict[str, Any] | None:
        """Sell a whole real position at market. Returns the fill, or None if it couldn't sell now."""
        book = book_id(connection_id)
        key = f"{book}/{symbol}"
        if key in self._selling:
            return None
        self._selling.add(key)
        conn = self.store.get_connection(connection_id)
        connector = None
        try:
            pos = self.trading.position(book, symbol)
            if conn is None or pos is None:
                return None
            ex = conn["exchange"].upper()
            connector = self._connector(conn)
            base = symbol.split("-")[0]
            balances = await connector.get_balances()
            have = next((Decimal(b.available) for b in balances if b.currency == base), Decimal(0))
            pair = self._pair(conn, symbol)
            qty = _floor(min(pos["qty"], have), Decimal(pair["lot_size"]) if pair.get("lot_size") else None)
            if qty <= 0 or (pair.get("min_size") and qty < Decimal(pair["min_size"])):
                self.trading.remove_position(book, symbol)
                self.store.audit(connection_id, "live_position_missing", f"{symbol} app qty {pos['qty']} exchange {have}")
                self.store.add_alert(connection_id, "critical",
                                     f"REAL: the {base} for your {symbol} trade is no longer in your {ex} account (only {fmt_num(have)} left), "
                                     "so it was removed from the app's list. Was it sold on the exchange directly?")
                return None
            fill = await self._send(connector, symbol, "sell", qty)
            if fill.filled_qty <= 0 or fill.avg_price is None:
                raise PortalError(f"sell order {fill.order_id} was not filled ({fill.status})")
            closed = self.trading.apply_close(book, symbol, fill.avg_price, Decimal(0), reason, fee=fill.fee_quote, sold_qty=fill.filled_qty)
            self._sell_after.pop(key, None)
            self.store.audit(connection_id, f"live_{reason}", f"{fill.filled_qty} {symbol} @ {fill.avg_price} order {fill.order_id}")
            label = {"stop_loss": "Stop-loss hit", "take_profit": "Take-profit hit", "manual_close": "Sold by you",
                     "strategy_exit": "Auto-trade sold (trend weakened)"}.get(reason, reason)
            pnl = Decimal(closed["realized_pnl"]) if closed else Decimal(0)
            self.store.add_alert(connection_id, "warning" if pnl < 0 else "info",
                                 f"REAL: {label} on {ex} {symbol}: sold {fmt_num(fill.filled_qty)} at {fmt_num(fill.avg_price)}, "
                                 f"profit/loss {pnl:+.2f} {PAPER_QUOTE}." + (f" {why}" if why else ""))
            try:
                await self._refresh_balances(conn, connector)
            except ExchangeError:
                pass
            return closed
        except (ExchangeError, PortalError) as exc:
            msg = exc.plain_message if isinstance(exc, ExchangeError) else exc.message
            if key not in self._sell_after:  # one alert per failure streak, then quiet retries
                self.store.add_alert(connection_id, "critical",
                                     f"REAL: couldn't sell {symbol} ({reason}): {msg} The app keeps retrying every {SELL_RETRY_SECONDS} seconds.")
            self._sell_after[key] = time.time() + SELL_RETRY_SECONDS
            log.warning("Real sell failed for %s: %s", symbol, type(exc).__name__)
            return None
        finally:
            self._selling.discard(key)
            if connector is not None:
                await connector.aclose()

    async def close_position(self, connection_id: str, symbol: str) -> dict[str, Any]:
        if self.trading.position(book_id(connection_id), symbol) is None:
            raise PortalError("No open real position for that pair.", 404)
        fill = await self.sell(connection_id, symbol, "manual_close")
        if fill is None:
            raise PortalError("The sell didn't go through. See the alerts for the exchange's answer; the app keeps retrying.", 502)
        return fill

    def on_ticker(self, t: Ticker) -> None:
        """Stop-loss and take-profit for real positions, checked on every price."""
        for pos in self.trading.positions():
            if not pos["connection_id"].endswith("#live") or pos["symbol"] != t.symbol:
                continue
            cid = pos["connection_id"][:-5]
            conn = self.store.get_connection(cid)
            if conn is None or conn["exchange"] != t.exchange:
                continue
            reason = "stop_loss" if t.bid <= pos["stop_price"] else \
                "take_profit" if pos["take_profit"] is not None and t.bid >= pos["take_profit"] else None
            key = f"{pos['connection_id']}/{t.symbol}"
            if reason is None or key in self._selling or time.time() < self._sell_after.get(key, 0):
                continue
            self._selling_soon(cid, t.symbol, reason)

    def _selling_soon(self, cid: str, symbol: str, reason: str) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self.sell(cid, symbol, reason))
        except RuntimeError:  # called from another thread: hand it to the server's loop
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self.sell(cid, symbol, reason), self._loop)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Let in-flight sells finish (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
