"""Trend breakout strategy with an over-extension ("don't chase") filter.

Entry conditions (all on the last closed candle):
  * Uptrend: close above the slow EMA and fast EMA above slow EMA.
  * Breakout: close above the highest high of the previous N candles (resistance).
  * Volume: breakout candle volume at least K × the average of the previous N.
  * Momentum: RSI between rsi_min and rsi_max.

Over-extension (any one blocks the entry):
  * Price more than X × ATR above the breakout level.
  * Price more than Y × ATR above the fast EMA.
  * RSI above rsi_max.
  * Momentum fading: RSI falling from high levels on lower volume.

The strategy only describes the market. It never places orders: its signal
goes to the decision gate like any other input.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.exchanges.base import TIMEFRAME_SECONDS, Candle

from .config import StrategyConfig
from .indicators import atr, ema, rsi


@dataclass
class Signal:
    symbol: str
    timeframe: str
    status: str  # BUY_SETUP | NO_SETUP | INSUFFICIENT_DATA | STALE_DATA
    entry_conditions_met: bool
    over_extended: bool | None
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    suggested_stop: float | None = None
    suggested_target: float | None = None
    candle_time_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(x, nd)


def evaluate(symbol: str, candles: list[Candle], cfg: StrategyConfig, live_price: float | None = None,
             now: float | None = None) -> Signal:
    tf_s = TIMEFRAME_SECONDS[cfg.timeframe]
    if len(candles) < cfg.candles_needed - 25:
        return Signal(symbol, cfg.timeframe, "INSUFFICIENT_DATA", False, None,
                      [f"Only {len(candles)} closed candles; need about {cfg.candles_needed - 25}."])
    last = candles[-1]
    now = now or time.time()
    close_time = last.open_time_ms / 1000 + tf_s
    if now - close_time > tf_s * cfg.max_candle_age_multiple:
        return Signal(symbol, cfg.timeframe, "STALE_DATA", False, None,
                      [f"Latest closed candle ended {int((now - close_time) / 60)} minutes ago; candle data is stale."],
                      candle_time_ms=last.open_time_ms)

    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    vols = [float(c.volume) for c in candles]
    ema_f = ema(closes, cfg.ema_fast)[-1]
    ema_s = ema(closes, cfg.ema_slow)[-1]
    rsi_series = rsi(closes, cfg.rsi_period)
    rsi_now, rsi_prev = rsi_series[-1], rsi_series[-2]
    atr_now = atr(highs, lows, closes, cfg.atr_period)[-1]
    window = slice(-cfg.breakout_lookback - 1, -1)
    resistance = max(highs[window])
    support = min(lows[window])
    avg_vol = sum(vols[window]) / cfg.breakout_lookback
    close = closes[-1]
    price = live_price if live_price is not None else close

    trend_up = close > ema_s and ema_f > ema_s
    breakout = close > resistance
    vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 0.0
    vol_ok = vol_ratio >= cfg.volume_multiple
    rsi_ok = cfg.rsi_min <= rsi_now <= cfg.rsi_max

    reasons = [
        ("✓ " if trend_up else "✗ ") + f"Trend: close {close:.6g} vs EMA{cfg.ema_slow} {ema_s:.6g}, EMA{cfg.ema_fast} {ema_f:.6g}",
        ("✓ " if breakout else "✗ ") + f"Breakout: close {close:.6g} vs {cfg.breakout_lookback}-candle resistance {resistance:.6g}",
        ("✓ " if vol_ok else "✗ ") + f"Volume: {vol_ratio:.2f}× average (need {cfg.volume_multiple}×)",
        ("✓ " if rsi_ok else "✗ ") + f"Momentum: RSI {rsi_now:.1f} (need {cfg.rsi_min:g}-{cfg.rsi_max:g})",
    ]
    entry = trend_up and breakout and vol_ok and rsi_ok

    ext_reasons = []
    above_breakout_atr = (price - resistance) / atr_now if atr_now else 0.0
    above_ema_atr = (price - ema_f) / atr_now if atr_now else 0.0
    if price > resistance and above_breakout_atr > cfg.max_breakout_extension_atr:
        ext_reasons.append(f"Price is {above_breakout_atr:.2f} ATR above the breakout level (max {cfg.max_breakout_extension_atr})")
    if above_ema_atr > cfg.max_ema_extension_atr:
        ext_reasons.append(f"Price is {above_ema_atr:.2f} ATR above EMA{cfg.ema_fast} (max {cfg.max_ema_extension_atr})")
    if rsi_now > cfg.rsi_max:
        ext_reasons.append(f"RSI {rsi_now:.1f} is above {cfg.rsi_max:g}")
    if rsi_prev is not None and rsi_now > 65 and rsi_now < rsi_prev and vols[-1] < vols[-2]:
        ext_reasons.append("Momentum fading: RSI falling from a high level on lower volume")
    extended = bool(ext_reasons)
    reasons += ["✗ Over-extended: " + r for r in ext_reasons] or ["✓ Not over-extended"]

    return Signal(
        symbol=symbol,
        timeframe=cfg.timeframe,
        status="BUY_SETUP" if entry and not extended else "NO_SETUP",
        entry_conditions_met=entry,
        over_extended=extended,
        reasons=reasons,
        metrics={
            "close": _r(close), "price": _r(price), "ema_fast": _r(ema_f), "ema_slow": _r(ema_s),
            "rsi": _r(rsi_now, 2), "atr": _r(atr_now), "support": _r(support), "resistance": _r(resistance),
            "volume_ratio": _r(vol_ratio, 2), "above_breakout_atr": _r(above_breakout_atr, 2),
            "above_ema_atr": _r(above_ema_atr, 2), "trend_up": trend_up, "breakout": breakout,
        },
        suggested_stop=_r(price - cfg.stop_atr * atr_now) if atr_now else None,
        suggested_target=_r(price + cfg.target_atr * atr_now) if atr_now else None,
        candle_time_ms=last.open_time_ms,
    )
