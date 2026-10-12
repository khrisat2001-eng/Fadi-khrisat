"""Fake connector used by service and API tests (no network, no real keys)."""
from __future__ import annotations

from decimal import Decimal

from app.exchanges.base import (
    Balance,
    ErrorKind,
    ExchangeConnector,
    ExchangeError,
    KeyInfo,
    OrderResult,
    Permission,
    TradingPair,
    WizardGuide,
)
from app.exchanges.okx import OkxConnector


class FakeExchange:
    """Shared behaviour for every FakeConnector instance, controlled by tests."""

    def __init__(self):
        self.permissions = {Permission.READ}
        self.raw = ["read_only"]
        self.ip_restricted = True
        self.error: ExchangeError | None = None
        self.valid_keys: set[str] | None = None  # None = every key valid
        self.seen_credentials = []
        # Real-mode simulation: balances move when orders fill at fill_price.
        self.balances = {"BTC": Decimal("0.5"), "USDT": Decimal("1000"), "ETH": Decimal("0")}
        self.fill_price = Decimal("100")
        self.fee_rate = Decimal("0.001")
        self.orders: dict[str, OrderResult] = {}
        self.sent: list[tuple] = []
        self.order_error: ExchangeError | None = None
        self.lose_answer = False  # the order goes through but the reply is lost

    def factory(self, exchange_id, creds):
        return FakeConnector(self, creds)


class FakeConnector(ExchangeConnector):
    exchange_id = "okx"

    def __init__(self, fx: FakeExchange, creds):
        self.fx = fx
        self.creds = creds
        fx.seen_credentials.append(creds)

    @classmethod
    def guide(cls) -> WizardGuide:
        return OkxConnector.guide()

    def _check(self):
        if self.fx.error:
            raise self.fx.error
        if self.fx.valid_keys is not None and self.creds.api_key not in self.fx.valid_keys:
            from app.exchanges.base import ErrorKind
            raise ExchangeError(ErrorKind.INVALID_KEY, "50111")

    async def get_key_info(self):
        self._check()
        return KeyInfo(set(self.fx.permissions), list(self.fx.raw), self.fx.ip_restricted, "uid-1")

    async def get_balances(self):
        self._check()
        return [Balance(c, str(v), str(v)) for c, v in self.fx.balances.items()]

    async def get_trading_pairs(self):
        return [TradingPair("BTC-USDT", "BTC", "USDT", min_size="0.0001", lot_size="0.0001"),
                TradingPair("ETH-USDT", "ETH", "USDT", min_size="0.001", lot_size="0.001")]

    async def place_market_order(self, symbol, side, qty, client_id):
        self._check()
        fx = self.fx
        fx.sent.append((symbol, side, qty, client_id))
        if fx.order_error:
            raise fx.order_error
        base = symbol.split("-")[0]
        px = fx.fill_price
        fee = qty * px * fx.fee_rate
        if side == "buy":
            if qty * px + fee > fx.balances["USDT"]:
                raise ExchangeError(ErrorKind.UNEXPECTED, "51008", "Insufficient balance")
            fx.balances["USDT"] -= qty * px + fee
            fx.balances[base] = fx.balances.get(base, Decimal(0)) + qty
        else:
            fx.balances[base] -= qty
            fx.balances["USDT"] += qty * px - fee
        oid = f"o{len(fx.orders) + 1}"
        fx.orders[oid] = OrderResult(oid, client_id, "filled", qty, px, fee)
        if fx.lose_answer:
            raise ExchangeError(ErrorKind.NETWORK, detail="ReadTimeout")
        return oid

    async def get_order(self, symbol, order_id=None, client_id=None):
        if order_id:
            return self.fx.orders.get(order_id)
        return next((o for o in self.fx.orders.values() if o.client_id == client_id), None)

    async def aclose(self):
        pass
