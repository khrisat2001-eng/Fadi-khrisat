"""Deterministic candle series for strategy tests."""
import math
import time
from decimal import Decimal

from app.exchanges.base import Candle


def make_candles(pattern="breakout", n=120, tf=900, end=None, breakout_jump=0.6, last_volume=3.0):
    """Closed candles oldest first, the last one ending just before `end`."""
    end = end or time.time()
    last_open = int((end - tf) // tf * tf)  # most recent fully closed candle
    out = []
    price = 100.0
    for i in range(n):
        t = last_open - (n - 1 - i) * tf
        wiggle = math.sin(i * 1.7) * 0.3
        if pattern == "down":
            price = 130 - 0.25 * i + wiggle
        elif i < n - 21:
            price = 100 + 0.25 * i + wiggle          # steady uptrend
        elif i < n - 1:
            price = 100 + 0.25 * (n - 21) + wiggle   # consolidation below resistance
        else:
            top = 100 + 0.25 * (n - 21) + 0.3
            price = top + breakout_jump if pattern == "breakout" else top - 1
        o = price - 0.1
        h = price + 0.3
        l = price - 0.4
        v = 100 * (last_volume if i == n - 1 else 1 + 0.1 * math.sin(i))
        out.append(Candle(t * 1000, Decimal(f"{o:.4f}"), Decimal(f"{h:.4f}"), Decimal(f"{l:.4f}"), Decimal(f"{price:.4f}"), Decimal(f"{v:.2f}")))
    return out
