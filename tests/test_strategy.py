"""Indicators, the breakout strategy, candle parsing and caching."""
import time

import httpx
import pytest

from app.exchanges.base import ErrorKind, ExchangeError
from app.exchanges.kucoin import KucoinConnector
from app.exchanges.okx import OkxConnector
from app.strategy.breakout import evaluate
from app.strategy.config import StrategyConfig
from app.strategy.indicators import atr, ema, rsi, sma
from app.strategy.service import CandleService, StrategyService

from .candles import make_candles

CFG = StrategyConfig()


# --- indicators -------------------------------------------------------------------
def test_ema_and_sma():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2.0 and e[3] == pytest.approx(3.0) and e[4] == pytest.approx(4.0)


def test_rsi_extremes_and_range():
    assert rsi([float(i) for i in range(30)], 14)[-1] == 100.0
    assert rsi([float(30 - i) for i in range(30)], 14)[-1] == pytest.approx(0.0)
    vals = [v for v in rsi([100 + (i % 3) - 1 for i in range(50)], 14) if v is not None]
    assert all(0 <= v <= 100 for v in vals)


def test_atr_constant_range():
    highs, lows, closes = [11.0] * 30, [9.0] * 30, [10.0] * 30
    assert atr(highs, lows, closes, 14)[-1] == pytest.approx(2.0)


# --- strategy ---------------------------------------------------------------------
def test_breakout_setup():
    s = evaluate("BTC-USDT", make_candles("breakout"), CFG)
    assert s.status == "BUY_SETUP" and s.entry_conditions_met and s.over_extended is False
    m = s.metrics
    assert m["close"] > m["resistance"] > m["support"]
    assert s.suggested_stop < m["close"] < s.suggested_target
    assert s.suggested_stop == pytest.approx(m["close"] - 2 * m["atr"], rel=1e-6)


@pytest.mark.parametrize("pattern,failing", [("down", "Trend"), ("flat", "Breakout")])
def test_no_setup(pattern, failing):
    s = evaluate("BTC-USDT", make_candles(pattern), CFG)
    assert s.status == "NO_SETUP" and not s.entry_conditions_met
    assert any(r.startswith("✗ " + failing) for r in s.reasons)


def test_low_volume_breakout_is_not_a_setup():
    s = evaluate("BTC-USDT", make_candles("breakout", last_volume=1.0), CFG)
    assert not s.entry_conditions_met
    assert any(r.startswith("✗ Volume") for r in s.reasons)


def test_chasing_far_above_breakout_is_blocked():
    candles = make_candles("breakout")
    s = evaluate("BTC-USDT", candles, CFG, live_price=float(candles[-1].close) + 3)
    assert s.entry_conditions_met and s.over_extended and s.status == "NO_SETUP"
    assert any("ATR above the breakout level" in r for r in s.reasons)


def test_big_breakout_candle_is_over_extended():
    s = evaluate("BTC-USDT", make_candles("breakout", breakout_jump=3.0), CFG)
    assert s.over_extended


def test_insufficient_and_stale_data():
    assert evaluate("X", make_candles(n=30), CFG).status == "INSUFFICIENT_DATA"
    old = make_candles(end=time.time() - 3 * 3600)
    s = evaluate("X", old, CFG)
    assert s.status == "STALE_DATA" and s.over_extended is None


def test_config_validation():
    with pytest.raises(ValueError):
        StrategyConfig(ema_fast=50, ema_slow=20)
    with pytest.raises(ValueError):
        StrategyConfig(timeframe="3m")


# --- candle parsing ---------------------------------------------------------------
async def test_okx_candles_keep_closed_only_and_sort():
    now_ms = int(time.time() // 900 * 900 * 1000)

    def handler(req):
        assert req.url.params["bar"] == "1H" and "OK-ACCESS-KEY" not in req.headers
        return httpx.Response(200, json={"code": "0", "data": [
            [str(now_ms), "3", "4", "2", "3.5", "10", "0", "0", "0"],          # still forming
            [str(now_ms - 3600000), "2", "3.2", "1.5", "3", "20", "0", "0", "1"],
            [str(now_ms - 7200000), "1", "2.1", "0.9", "2", "30", "0", "0", "1"],
        ]})

    c = OkxConnector(None, client=httpx.AsyncClient(base_url=OkxConnector.base_url, transport=httpx.MockTransport(handler)))
    candles = await c.get_candles("BTC-USDT", "1h", 3)
    assert [str(x.close) for x in candles] == ["2", "3"]
    assert str(candles[0].high) == "2.1" and str(candles[0].low) == "0.9"


async def test_kucoin_candles_column_order():
    step = 900
    end = int(time.time())
    closed = (end // step - 1) * step

    def handler(req):
        assert req.url.params["type"] == "15min"
        # KuCoin rows: time, open, close, high, low, volume, turnover (newest first)
        return httpx.Response(200, json={"code": "200000", "data": [
            [str(closed + step), "5", "6", "7", "4", "1", "0"],  # forming
            [str(closed), "1", "2", "3", "0.5", "9", "0"],
        ]})

    c = KucoinConnector(None, client=httpx.AsyncClient(base_url=KucoinConnector.base_url, transport=httpx.MockTransport(handler)))
    [k] = await c.get_candles("BTC-USDT", "15m", 5)
    assert (str(k.open), str(k.high), str(k.low), str(k.close), str(k.volume)) == ("1", "3", "0.5", "2", "9")


# --- service ----------------------------------------------------------------------
async def test_candle_cache_avoids_refetching():
    calls = []

    async def fetch(ex, sym, tf, limit):
        calls.append(1)
        return make_candles()[-limit:]

    svc = CandleService(fetch)
    await svc.get("okx", "BTC-USDT", "15m", 100)
    await svc.get("okx", "BTC-USDT", "15m", 100)
    assert len(calls) == 1


async def test_candle_errors_become_stale_signal():
    async def fetch(*a):
        raise ExchangeError(ErrorKind.NETWORK)

    s = await StrategyService(CandleService(fetch), lambda: CFG).signal("okx", "BTC-USDT")
    assert s.status == "STALE_DATA" and "couldn't reach" in s.reasons[0].lower()
