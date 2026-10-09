from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Ticker:
    exchange: str
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    exchange_ts_ms: int
    received_at: float = field(default_factory=time.time)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        if self.mid == 0:
            return Decimal("Infinity")
        return (self.ask - self.bid) / self.mid * Decimal(10000)

    def age_seconds(self, now: float | None = None) -> float:
        return (now or time.time()) - self.received_at
