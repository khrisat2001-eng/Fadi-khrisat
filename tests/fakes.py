"""Fake connector used by service and API tests (no network, no real keys)."""
from __future__ import annotations

from app.exchanges.base import Balance, ExchangeConnector, ExchangeError, KeyInfo, Permission, TradingPair, WizardGuide
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
        return [Balance("BTC", "0.5", "0.4"), Balance("USDT", "1000", "1000"), Balance("ETH", "0", "0")]

    async def get_trading_pairs(self):
        return [TradingPair("BTC-USDT", "BTC", "USDT"), TradingPair("ETH-USDT", "ETH", "USDT")]

    async def aclose(self):
        pass
