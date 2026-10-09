"""Candle cache and strategy evaluation for connections' pairs."""
from __future__ import annotations

import time
from typing import Awaitable, Callable

from app.exchanges.base import TIMEFRAME_SECONDS, Candle, ExchangeError
from app.exchanges.registry import get_connector_class

from .breakout import Signal, evaluate
from .config import StrategyConfig

CandleFetcher = Callable[[str, str, str, int], Awaitable[list[Candle]]]


async def fetch_public_candles(exchange: str, symbol: str, timeframe: str, limit: int) -> list[Candle]:
    connector = get_connector_class(exchange)(None)
    try:
        return await connector.get_candles(symbol, timeframe, limit)
    finally:
        await connector.aclose()


class CandleService:
    def __init__(self, fetch: CandleFetcher = fetch_public_candles):
        self._fetch = fetch
        self._cache: dict[tuple[str, str, str], tuple[float, list[Candle]]] = {}

    async def get(self, exchange: str, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        key = (exchange, symbol, timeframe)
        now = time.time()
        hit = self._cache.get(key)
        if hit:
            fetched_at, candles = hit
            next_close = candles[-1].open_time_ms / 1000 + 2 * TIMEFRAME_SECONDS[timeframe] if candles else 0
            # Refetch once a newer candle should have closed, at most every 30s.
            if len(candles) >= limit and (now < next_close or now - fetched_at < 30):
                return candles
        candles = await self._fetch(exchange, symbol, timeframe, limit)
        self._cache[key] = (now, candles)
        return candles


class StrategyService:
    def __init__(self, candles: CandleService, get_config: Callable[[], StrategyConfig]):
        self.candles = candles
        self.get_config = get_config

    async def signal(self, exchange: str, symbol: str, live_price: float | None = None) -> Signal:
        cfg = self.get_config()
        try:
            candles = await self.candles.get(exchange, symbol, cfg.timeframe, cfg.candles_needed)
        except (ExchangeError, ValueError, OSError) as exc:
            message = exc.plain_message if isinstance(exc, ExchangeError) else str(exc)
            return Signal(symbol, cfg.timeframe, "STALE_DATA", False, None, [f"Couldn't load candles: {message}"])
        return evaluate(symbol, candles, cfg, live_price)
