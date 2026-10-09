"""Decision gate: every mandatory check, sizing, and approval tokens."""
import time
from decimal import Decimal as D

import pytest

from app.marketdata.models import Ticker
from app.risk.config import RiskConfig
from app.news.rules import NewsAssessment
from app.risk.gate import DecisionGate, GateContext, TradeRequest

CFG = RiskConfig()
SETUP = {"status": "BUY_SETUP", "timeframe": "15m", "entry_conditions_met": True, "over_extended": False,
         "reasons": ["✓ Trend: up", "✓ Breakout: yes", "✓ Volume: 2×", "✓ Momentum: RSI 60", "✓ Not over-extended"]}
NO_SETUP = {"status": "NO_SETUP", "timeframe": "15m", "entry_conditions_met": False, "over_extended": False,
            "reasons": ["✓ Trend: up", "✗ Breakout: close 99 vs resistance 101", "✓ Volume", "✓ Momentum", "✓ Not over-extended"]}
EXTENDED = {"status": "NO_SETUP", "timeframe": "15m", "entry_conditions_met": True, "over_extended": True,
            "reasons": ["✓ Trend", "✓ Breakout", "✓ Volume", "✓ Momentum", "✗ Over-extended: Price is 3.0 ATR above the breakout level (max 1.0)"]}
STALE = {"status": "STALE_DATA", "timeframe": "15m", "entry_conditions_met": False, "over_extended": None,
         "reasons": ["Latest closed candle ended 90 minutes ago; candle data is stale."]}


def ticker(bid="100", ask="100.05", age=0.5, symbol="BTC-USDT"):
    return Ticker("okx", symbol, D(bid), D(ask), D(bid), 0, received_at=time.time() - age)


def ctx(**over):
    base = dict(
        connection={"id": "c1", "exchange": "okx", "state": "PAPER", "health": "ok", "selected_pairs": ["BTC-USDT", "ETH-USDT", "ETH-BTC"]},
        ticker=ticker(), stream_state="connected",
        account={"cash": D("10000"), "starting_balance": D("10000")},
        positions=[], marks={}, realized_pnl_today=D(0), suspensions=[], kill_switch=False,
        signal=SETUP, news=NewsAssessment("pass", "No relevant news."),
    )
    base.update(over)
    return GateContext(**base)


def req(**over):
    base = dict(connection_id="c1", symbol="BTC-USDT", side="buy", stop_price=D("98"), take_profit_price=D("106"))
    base.update(over)
    return TradeRequest(**base)


def check(decision, name):
    return next(c for c in decision.checks if c.name == name)


gate = DecisionGate()


def test_approves_valid_trade_and_sizes_by_risk():
    d = gate.evaluate(req(), ctx(), CFG)
    assert d.status == "APPROVE", d.summary
    assert d.token
    # Risk 1% of 10,000 = 100. Per-unit risk ≈ entry cost - stop net ≈ 2.29, so ~43.6 units,
    # but the 20% max position (2,000) caps it at ~19.97 units.
    assert D("19.9") < d.qty < D("20")
    assert "max position size" in check(d, "sizing").detail
    assert check(d, "news").result == "pass"
    assert check(d, "technical").result == "pass"
    assert check(d, "extension").result == "pass"


def test_risk_limits_size_when_stop_is_wide():
    d = gate.evaluate(req(stop_price=D("90"), take_profit_price=D("130")), ctx(), CFG)
    assert d.status == "APPROVE"
    assert "risk per trade" in check(d, "sizing").detail
    loss_at_stop = d.qty * (d.est_entry_price * D("1.001") - D("90") * D("0.9995") * D("0.999"))
    assert loss_at_stop <= D("100.0001")


@pytest.mark.parametrize("over,name,status", [
    ({"kill_switch": True}, "kill_switch", "REJECT"),
    ({"connection": {"id": "c1", "exchange": "okx", "state": "CONNECTED_READONLY", "health": "ok", "selected_pairs": ["BTC-USDT"]}}, "connection", "REJECT"),
    ({"connection": {"id": "c1", "exchange": "okx", "state": "PAPER", "health": "auth_failed", "selected_pairs": ["BTC-USDT"]}}, "connection", "REJECT"),
    ({"ticker": None}, "market_data", "WAIT"),
    ({"ticker": ticker(age=60)}, "market_data", "WAIT"),
    ({"stream_state": "reconnecting"}, "market_data", "WAIT"),
    ({"ticker": ticker(bid="99", ask="100")}, "liquidity", "WAIT"),
    ({"suspensions": [{"scope": "asset", "target": "BTC", "reason": "exploit report"}]}, "suspensions", "REJECT"),
    ({"suspensions": [{"scope": "exchange", "target": "okx", "reason": "maintenance"}]}, "suspensions", "REJECT"),
    ({"suspensions": [{"scope": "global", "target": "*", "reason": "FOMC"}]}, "suspensions", "REJECT"),
    ({"realized_pnl_today": D("-300")}, "portfolio", "REJECT"),
    ({"account": None}, "account", "REJECT"),
])
def test_blocking_conditions(over, name, status):
    d = gate.evaluate(req(), ctx(**over), CFG)
    assert d.status == status
    assert check(d, name).result in ("fail", "wait")
    assert d.token is None and d.qty is None


def test_unrelated_suspension_does_not_block():
    d = gate.evaluate(req(), ctx(suspensions=[{"scope": "asset", "target": "ETH", "reason": "x"}]), CFG)
    assert d.status == "APPROVE"


@pytest.mark.parametrize("over,detail", [
    ({"stop_price": D("100.5")}, "below the current bid"),
    ({"stop_price": D("99.8")}, "normal noise"),
    ({"take_profit_price": D("100")}, "above the expected entry"),
])
def test_invalid_protective_exits(over, detail):
    d = gate.evaluate(req(**over), ctx(), RiskConfig(min_stop_distance_bps=D("50")))
    assert d.status == "REJECT"
    assert detail in check(d, "protective_exit").detail
    assert check(d, "reward_risk").result == "skipped"


def test_reward_risk_is_net_of_costs():
    # Gross 2:1 (risk 2, reward 4) but fees+slippage push it below a 2.0 minimum.
    cfg = RiskConfig(min_reward_risk=D("2"))
    d = gate.evaluate(req(stop_price=D("98"), take_profit_price=D("104")), ctx(), cfg)
    assert d.status == "REJECT"
    assert check(d, "reward_risk").result == "fail"


def test_max_open_positions():
    positions = [{"symbol": f"C{i}-USDT", "qty": D(1), "avg_price": D(1)} for i in range(5)]
    d = gate.evaluate(req(), ctx(positions=positions), CFG)
    assert check(d, "portfolio").result == "fail"


def test_unrealized_losses_count_toward_daily_limit():
    positions = [{"symbol": "ETH-USDT", "qty": D(10), "avg_price": D(100)}]
    d = gate.evaluate(req(), ctx(positions=positions, marks={"ETH-USDT": D(70)}), CFG)  # -300 unrealized
    assert check(d, "portfolio").result == "fail"


def test_existing_position_counts_toward_size_cap():
    positions = [{"symbol": "BTC-USDT", "qty": D(19), "avg_price": D(100)}]
    account = {"cash": D("8100"), "starting_balance": D("10000")}  # equity 10,000, 1,900 already in BTC
    d = gate.evaluate(req(), ctx(account=account, positions=positions, marks={"BTC-USDT": D(100)}), CFG)
    assert d.status == "APPROVE"
    assert d.qty < D("1")  # only 100 of the 2,000 cap is left


def test_non_usdt_and_unselected_pairs():
    assert gate.evaluate(req(symbol="ETH-BTC"), ctx(ticker=ticker(symbol="ETH-BTC")), CFG).status == "REJECT"
    assert gate.evaluate(req(symbol="SOL-USDT"), ctx(ticker=ticker(symbol="SOL-USDT")), CFG).status == "REJECT"


def test_only_buys_open_positions():
    assert gate.evaluate(req(side="sell"), ctx(), CFG).status == "REJECT"


def test_strategy_orders_need_a_setup():
    d = gate.evaluate(req(source="strategy"), ctx(signal=NO_SETUP), CFG)
    assert d.status == "REJECT" and "Breakout" in check(d, "technical").detail
    assert gate.evaluate(req(source="strategy"), ctx(), CFG).status == "APPROVE"


def test_manual_orders_without_setup_warn_but_pass():
    d = gate.evaluate(req(), ctx(signal=NO_SETUP), CFG)
    assert d.status == "APPROVE"
    assert check(d, "technical").result == "warn"


@pytest.mark.parametrize("source", ["manual", "strategy"])
def test_over_extended_blocks_every_order(source):
    d = gate.evaluate(req(source=source), ctx(signal=EXTENDED), CFG)
    assert d.status == "REJECT"
    assert "Don't chase" in check(d, "extension").detail


@pytest.mark.parametrize("signal", [None, STALE])
def test_missing_or_stale_candles_wait(signal):
    d = gate.evaluate(req(), ctx(signal=signal), CFG)
    assert d.status == "WAIT"
    assert check(d, "technical").result == "wait"


@pytest.mark.parametrize("over", [
    {"kill_switch": True},
    {"realized_pnl_today": D("-1000")},
    {"suspensions": [{"scope": "asset", "target": "BTC", "reason": "hack"}]},
    {"ticker": ticker(age=60)},
])
def test_strong_signals_cannot_bypass_risk_controls(over):
    """A maximal 'news'/strategy signal with override flags changes nothing when a safety check fails."""
    signal = {**SETUP, "news_score": 1.0, "confidence": 1.0, "override_risk": True, "force": True, "bypass_gate": True}
    d = gate.evaluate(req(source="strategy", risk_pct=D("5")), ctx(**{"signal": signal, **over}), CFG)
    assert d.status != "APPROVE" and d.token is None


def test_risk_pct_cannot_exceed_config():
    big = gate.evaluate(req(stop_price=D("90"), take_profit_price=D("130"), risk_pct=D("5")), ctx(), CFG)
    normal = gate.evaluate(req(stop_price=D("90"), take_profit_price=D("130")), ctx(), CFG)
    assert big.qty == normal.qty


# --- tokens -----------------------------------------------------------------------
def test_token_verifies_only_for_exact_order():
    d = gate.evaluate(req(), ctx(), CFG)
    assert gate.verify(d.token, "c1", "BTC-USDT", "buy", d.qty) == d.id
    with pytest.raises(PermissionError):
        gate.verify(d.token, "c1", "BTC-USDT", "buy", d.qty * 2)
    with pytest.raises(PermissionError):
        gate.verify(d.token, "c2", "BTC-USDT", "buy", d.qty)
    with pytest.raises(PermissionError):
        DecisionGate().verify(d.token, "c1", "BTC-USDT", "buy", d.qty)  # different secret
    with pytest.raises(PermissionError):
        gate.verify(d.token.replace('"qty":"', '"qty":"9'), "c1", "BTC-USDT", "buy", d.qty)
    with pytest.raises(PermissionError):
        gate.verify("garbage", "c1", "BTC-USDT", "buy", d.qty)


def test_token_expires(monkeypatch):
    d = gate.evaluate(req(), ctx(), CFG)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 31)
    with pytest.raises(PermissionError, match="expired"):
        gate.verify(d.token, "c1", "BTC-USDT", "buy", d.qty)


# --- news (safety-only) ----------------------------------------------------------------

def test_news_wait_and_fail_block_entries():
    d = gate.evaluate(req(), ctx(news=NewsAssessment("wait", "Unverified report; verifying.")), RiskConfig())
    assert d.status == "WAIT" and d.token is None and "verifying" in d.summary
    d = gate.evaluate(req(), ctx(news=NewsAssessment("fail", "Confirmed exploit.")), RiskConfig())
    assert d.status == "REJECT" and d.token is None


def test_news_can_shrink_but_never_grow_size():
    cfg = RiskConfig(max_position_pct=D("100"))
    base = gate.evaluate(req(), ctx(), cfg).qty
    half = gate.evaluate(req(), ctx(news=NewsAssessment("warn", "Volatile.", size_multiplier=0.5)), cfg)
    assert half.status == "APPROVE" and abs(half.qty - base / 2) < D("0.0001")
    assert "Risk cut to 50%" in check(half, "sizing").detail
    bigger = gate.evaluate(req(), ctx(news=NewsAssessment("pass", "Great news!", size_multiplier=3.0)), cfg)
    assert bigger.qty == base


def test_news_cannot_override_other_checks():
    d = gate.evaluate(req(), ctx(kill_switch=True, news=NewsAssessment("pass", "Strongly positive.", size_multiplier=1.0)), RiskConfig())
    assert d.status == "REJECT"
    d = gate.evaluate(req(), ctx(suspensions=[{"scope": "asset", "target": "BTC", "reason": "x"}],
                                 news=NewsAssessment("pass", "ok")), RiskConfig())
    assert d.status == "REJECT"


def test_missing_news_engine_is_reported():
    d = gate.evaluate(req(), ctx(news=None), RiskConfig())
    assert check(d, "news").result == "warn" and d.status == "APPROVE"
