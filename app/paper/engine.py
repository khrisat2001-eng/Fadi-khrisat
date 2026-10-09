"""Paper trading: simulated fills on live prices, protective exits, account views.

Entries must carry a decision-gate approval token. Fills use the live ask/bid
plus configured slippage and taker fees. Stop-loss and take-profit are checked
on every ticker. Stops can be tightened but never widened.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.connections.service import ConnectionService, PortalError
from app.marketdata.models import Ticker
from app.marketdata.supervisor import MarketDataManager
from app.risk.config import RiskConfig
from app.risk.gate import PAPER_QUOTE, DecisionGate, GateContext, TradeRequest

from .store import TradingStore, utc_day_start_iso

log = logging.getLogger("app.paper")


class PaperTradingService:
    def __init__(self, connections: ConnectionService, trading: TradingStore, market: MarketDataManager, gate: DecisionGate):
        self.connections = connections
        self.store = connections.store
        self.trading = trading
        self.market = market
        self.gate = gate
        market.add_listener(self.on_ticker)

    # --- settings ----------------------------------------------------------------------
    def risk_config(self) -> RiskConfig:
        return RiskConfig(**(self.trading.get_setting("risk_config") or {}))

    def set_risk_config(self, cfg: RiskConfig) -> RiskConfig:
        self.trading.set_setting("risk_config", cfg.as_json())
        self.store.audit(None, "risk_config_changed", str(cfg.as_json()))
        return cfg

    def kill_switch(self) -> bool:
        return bool(self.trading.get_setting("kill_switch"))

    def set_kill_switch(self, on: bool, reason: str = "") -> None:
        self.trading.set_setting("kill_switch", on)
        self.store.audit(None, "kill_switch_on" if on else "kill_switch_off", reason)
        self.store.add_alert(None, "critical" if on else "info",
                             "Emergency stop turned ON: no new entries. Open positions keep their stops." if on
                             else "Emergency stop turned off.")

    # --- streams -----------------------------------------------------------------------
    def desired_streams(self) -> dict[str, set[str]]:
        want: dict[str, set[str]] = {}
        for c in self.store.list_connections():
            if c["state"] == "PAPER":
                want.setdefault(c["exchange"], set()).update(c["selected_pairs"])
        for p in self.trading.positions():
            c = self.store.get_connection(p["connection_id"])
            if c:  # keep watching prices of open positions, even if the pair was deselected
                want.setdefault(c["exchange"], set()).add(p["symbol"])
        return want

    async def sync_streams(self) -> None:
        await self.market.reconcile(self.desired_streams())

    # --- accounts ----------------------------------------------------------------------
    def _paper_connection(self, connection_id: str) -> dict[str, Any]:
        conn = self.store.get_connection(connection_id)
        if conn is None:
            raise PortalError("Connection not found.", 404)
        if conn["state"] != "PAPER":
            raise PortalError("Enable paper trading on this connection first.", 409)
        return conn

    def create_account(self, connection_id: str, starting_balance: Decimal | None) -> dict[str, Any]:
        conn = self._paper_connection(connection_id)
        if self.trading.get_account(connection_id):
            raise PortalError("This connection already has a paper account.", 409)
        if starting_balance is None:
            usdt = next((Decimal(b["total"]) for b in conn["balances"] if b["currency"] == PAPER_QUOTE), Decimal(0))
            starting_balance = (usdt * Decimal(str(conn["allocation_pct"] or 0)) / 100).quantize(Decimal("0.01"))
            if starting_balance <= 0:
                raise PortalError(f"Your real {PAPER_QUOTE} balance is zero, so enter a starting paper balance.")
        if starting_balance <= 0:
            raise PortalError("Starting balance must be above zero.")
        self.trading.create_account(connection_id, PAPER_QUOTE, starting_balance)
        self.store.audit(connection_id, "paper_account_created", f"{starting_balance} {PAPER_QUOTE}")
        return self.account_view(connection_id)

    def close_account(self, connection_id: str) -> None:
        self.trading.delete_account(connection_id)

    # --- entries -----------------------------------------------------------------------
    def build_context(self, conn: dict[str, Any], symbol: str) -> GateContext:
        account = self.trading.get_account(conn["id"])
        positions = self.trading.positions(conn["id"])
        marks = {}
        for p in positions:
            t = self.market.latest(conn["exchange"], p["symbol"])
            if t:
                marks[p["symbol"]] = t.bid
        min_size = next((p.get("min_size") for p in conn["available_pairs"] if p["symbol"] == symbol), None)
        return GateContext(
            connection=conn,
            ticker=self.market.latest(conn["exchange"], symbol),
            stream_state=self.market.stream_state(conn["exchange"]),
            account=account,
            positions=positions,
            marks=marks,
            realized_pnl_today=self.trading.realized_pnl_since(conn["id"], utc_day_start_iso()),
            suspensions=self.trading.active_suspensions(),
            kill_switch=self.kill_switch(),
            pair_min_size=Decimal(min_size) if min_size else None,
        )

    def place_order(self, req: TradeRequest) -> dict[str, Any]:
        conn = self.store.get_connection(req.connection_id)
        if conn is None:
            raise PortalError("Connection not found.", 404)
        cfg = self.risk_config()
        decision = self.gate.evaluate(req, self.build_context(conn, req.symbol), cfg)
        self.trading.add_decision({**decision.as_dict(), "connection_id": req.connection_id,
                                   "symbol": req.symbol, "side": req.side})
        result: dict[str, Any] = {"decision": decision.as_dict(), "fill": None}
        if decision.status == "APPROVE":
            result["fill"] = self.execute(decision.token, req, decision.qty, cfg)
        return result

    def execute(self, token: str | None, req: TradeRequest, qty: Decimal, cfg: RiskConfig) -> dict[str, Any]:
        """The only path that opens a paper position. Requires a matching approval token."""
        if not token:
            raise PermissionError("Orders need a decision-gate approval.")
        decision_id = self.gate.verify(token, req.connection_id, req.symbol, req.side, qty)
        conn = self.store.get_connection(req.connection_id)
        t = self.market.latest(conn["exchange"], req.symbol)
        price = t.ask * (1 + cfg.slippage_bps / Decimal(10000))
        fee = qty * price * cfg.taker_fee_rate
        self.trading.apply_buy(req.connection_id, req.symbol, qty, price, fee, req.stop_price, req.take_profit_price, decision_id)
        self.store.audit(req.connection_id, "paper_buy", f"{qty} {req.symbol} @ {price:.8f} decision {decision_id}")
        return {"side": "buy", "symbol": req.symbol, "qty": str(qty), "price": str(price), "fee": str(fee)}

    # --- exits -------------------------------------------------------------------------
    def _close(self, conn: dict[str, Any], symbol: str, bid: Decimal, reason: str) -> dict[str, Any] | None:
        cfg = self.risk_config()
        price = bid * (1 - cfg.slippage_bps / Decimal(10000))
        fill = self.trading.apply_close(conn["id"], symbol, price, cfg.taker_fee_rate, reason)
        if fill:
            self.store.audit(conn["id"], f"paper_{reason}", f"{fill['qty']} {symbol} @ {price:.8f} pnl {Decimal(fill['realized_pnl']):.2f}")
        return fill

    def close_position(self, connection_id: str, symbol: str) -> dict[str, Any]:
        conn = self.store.get_connection(connection_id)
        if conn is None or self.trading.position(connection_id, symbol) is None:
            raise PortalError("No open position for that pair.", 404)
        t = self.market.latest(conn["exchange"], symbol)
        if t is None or t.age_seconds() > self.risk_config().max_data_age_seconds:
            raise PortalError("No current price for this pair, so the paper close can't be priced. Try again when market data is back.", 409)
        return self._close(conn, symbol, t.bid, "manual_close")

    def update_stop(self, connection_id: str, symbol: str, new_stop: Decimal) -> dict[str, Any]:
        conn = self.store.get_connection(connection_id)
        pos = self.trading.position(connection_id, symbol)
        if conn is None or pos is None:
            raise PortalError("No open position for that pair.", 404)
        if new_stop <= pos["stop_price"]:
            raise PortalError(f"Stops can only be tightened. The current stop is {pos['stop_price']}; widening it is not allowed.")
        t = self.market.latest(conn["exchange"], symbol)
        if t is not None and new_stop >= t.bid:
            raise PortalError("The new stop must be below the current bid.")
        self.trading.set_stop(pos["id"], new_stop)
        self.store.audit(connection_id, "stop_tightened", f"{symbol} {pos['stop_price']} -> {new_stop}")
        return self.account_view(connection_id)

    def on_ticker(self, t: Ticker) -> None:
        for pos in self.trading.positions():
            if pos["symbol"] != t.symbol:
                continue
            conn = self.store.get_connection(pos["connection_id"])
            if conn is None or conn["exchange"] != t.exchange:
                continue
            reason = None
            if t.bid <= pos["stop_price"]:
                reason = "stop_loss"
            elif pos["take_profit"] is not None and t.bid >= pos["take_profit"]:
                reason = "take_profit"
            if reason:
                fill = self._close(conn, t.symbol, t.bid, reason)
                if fill:
                    label = "Stop-loss" if reason == "stop_loss" else "Take-profit"
                    self.store.add_alert(conn["id"], "warning" if reason == "stop_loss" else "info",
                                         f"Paper {label} hit on {conn['exchange'].upper()} {t.symbol}: sold {fill['qty']} at "
                                         f"{Decimal(fill['price']):.6f}, P&L {Decimal(fill['realized_pnl']):.2f} {PAPER_QUOTE}.")

    # --- views -------------------------------------------------------------------------
    def account_view(self, connection_id: str) -> dict[str, Any]:
        conn = self.store.get_connection(connection_id)
        if conn is None:
            raise PortalError("Connection not found.", 404)
        acct = self.trading.get_account(connection_id)
        if acct is None:
            return {"connection_id": connection_id, "account": None}
        positions = []
        equity = acct["cash"]
        for p in self.trading.positions(connection_id):
            t = self.market.latest(conn["exchange"], p["symbol"])
            mark = t.bid if t else p["avg_price"]
            value = p["qty"] * mark
            equity += value
            positions.append({
                "symbol": p["symbol"], "qty": str(p["qty"]), "avg_price": str(p["avg_price"]),
                "stop_price": str(p["stop_price"]), "take_profit": str(p["take_profit"]) if p["take_profit"] else None,
                "mark": str(mark), "mark_is_live": t is not None, "value": f"{value:.2f}",
                "unrealized_pnl": f"{p['qty'] * (mark - p['avg_price']):.2f}", "opened_at": p["opened_at"],
            })
        return {
            "connection_id": connection_id,
            "account": {
                "currency": acct["currency"],
                "starting_balance": f"{acct['starting_balance']:.2f}",
                "cash": f"{acct['cash']:.2f}",
                "equity": f"{equity:.2f}",
                "total_pnl": f"{equity - acct['starting_balance']:.2f}",
                "realized_pnl_today": f"{self.trading.realized_pnl_since(connection_id, utc_day_start_iso()):.2f}",
            },
            "positions": positions,
            "fills": self.trading.fills(connection_id, 50),
        }
