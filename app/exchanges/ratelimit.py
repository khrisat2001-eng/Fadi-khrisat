"""Async token-bucket rate limiter, one bucket per endpoint group."""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, capacity: int, per_seconds: float):
        self.capacity = capacity
        self.rate = capacity / per_seconds
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class RateLimiter:
    """Holds buckets keyed by endpoint group, e.g. "account", "public".

    Limits are set below each exchange's published limits to leave headroom.
    """

    def __init__(self, limits: dict[str, tuple[int, float]]):
        self._buckets = {name: TokenBucket(*spec) for name, spec in limits.items()}

    async def acquire(self, group: str) -> None:
        await self._buckets[group].acquire()
