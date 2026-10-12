"""Real mode against the fake exchange: unlock rules, budget limits, real fills, exits and Auto-trade."""
import asyncio
from decimal import Decimal as D

import pytest

from app.exchanges.base import ErrorKind, ExchangeError, Permission

from .test_paper_api import price, wait
from .test_portal_api import connect


def run(coro_fn, client):
    return asyncio.run(coro_fn(client))


@pytest.fixture
def real(client, fx):
    """A connection in practice mode with a trade-enabled key and the server switch on."""
    fx.permissions = {Permission.READ, Permission.TRADE}
    fx.raw = ["read_only", "trade"]
    cid = connect(client).json()["id"]
    client.put(f"/api/connections/{cid}/settings", json={"allocation_pct": 20, "pairs": ["BTC-USDT", "ETH-USDT"]})
    client.post(f"/api/connections/{cid}/paper")
    wait(lambda: client.app_ref.state.market.stream_state("okx") == "connected")
    client.app_ref.state.live.enabled = True
    return cid


def unlock(client, cid):
    return client.post(f"/api/live/{cid}/unlock", json={"confirm": "real money"})


def buy(client, cid, stop="98", tp="106"):
    return client.post(f"/api/live/{cid}/orders", json={"symbol": "BTC-USDT", "stop_price": stop, "take_profit_price": tp})


def test_locked_by_default_and_server_switch_required(client, real, fx):
    client.app_ref.state.live.enabled = False
    v = client.get(f"/api/live/{real}").json()
    assert v["unlocked"] is False and v["server_switch"] is False and v["account"] is None
    r = unlock(client, real)
    assert r.status_code == 403 and "LIVE_TRADING" in r.json()["error"]
    assert buy(client, real).status_code == 403
    assert fx.sent == []


def test_unlock_needs_the_phrase_and_a_trade_key(client, real, fx):
    assert unlock_wrong(client, real).status_code == 400
    client.app_ref.state.service.store.update_connection(real, permissions=["read"])
    r = unlock(client, real)
    assert r.status_code == 409 and "Trade permission" in r.json()["error"]
    assert client.post(f"/api/live/{real}/orders", json={"symbol": "BTC-USDT"}).status_code == 403


def unlock_wrong(client, cid):
    return client.post(f"/api/live/{cid}/unlock", json={"confirm": "yes"})


def test_unlock_sets_default_limits_and_marks_the_connection(client, real):
    v = unlock(client, real).json()
    assert v["unlocked"] is True and v["per_trade"] == "20" and v["budget"] == "100"
    assert v["account"]["starting_balance"] == "100.00"
    assert client.get(f"/api/connections/{real}").json()["live_trading"] is True
    home = client.get("/api/home").json()
    assert home["live_trading"] == "on" and home["real_money_exchanges"] == ["okx"]


def test_real_buy_is_capped_and_recorded_from_the_exchange_fill(client, real, fx):
    unlock(client, real)
    price(client, 100)
    r = buy(client, real).json()
    assert r["decision"]["status"] == "APPROVE", r["decision"]["summary"]
    [(symbol, side, qty, client_id)] = fx.sent
    assert symbol == "BTC-USDT" and side == "buy" and client_id.startswith("fk")
    assert qty * D("100.05") <= D("20") and qty == qty.quantize(D("0.0001"))  # per-trade limit, lot size
    v = client.get(f"/api/live/{real}").json()
    [pos] = v["positions"]
    assert D(pos["qty"]) == qty and D(pos["avg_price"]) == D("100")
    assert D(v["account"]["cash"]) < D("100")
    # Practice money is untouched.
    assert client.get(f"/api/paper/{real}").json()["account"] is None
    [dec] = client.get(f"/api/decisions?connection_id={real}%23live").json()
    assert dec["inputs"]["mode"] == "real"


def test_stop_loss_sells_on_the_exchange(client, real, fx):
    unlock(client, real)
    price(client, 100)
    buy(client, real)
    usdt_before = fx.balances["USDT"]
    fx.fill_price = D("97.9")
    price(client, 97.9)
    wait(lambda: client.get(f"/api/live/{real}").json()["positions"] == [])
    [_, (_, side, qty, _)] = fx.sent
    assert side == "sell" and fx.balances["BTC"] == D("0.5")
    assert fx.balances["USDT"] > usdt_before
    fills = client.get(f"/api/live/{real}").json()["fills"]
    assert fills[0]["reason"] == "stop_loss" and D(fills[0]["realized_pnl"]) < 0


def test_manual_sell_and_failed_sell_keeps_the_position(client, real, fx):
    unlock(client, real)
    price(client, 100)
    buy(client, real)
    fx.order_error = ExchangeError(ErrorKind.UNEXPECTED, "51000", "Parameter error")
    r = client.post(f"/api/live/{real}/positions/BTC-USDT/close")
    assert r.status_code == 502
    assert len(client.get(f"/api/live/{real}").json()["positions"]) == 1
    alerts = client.get("/api/alerts").json()
    assert any("couldn't sell" in a["message"] for a in alerts)
    fx.order_error = None
    client.app_ref.state.live._sell_after.clear()
    assert client.post(f"/api/live/{real}/positions/BTC-USDT/close").json()["reason"] == "manual_close"
    assert client.get(f"/api/live/{real}").json()["positions"] == []


def test_lost_reply_is_found_by_client_id_and_never_resent(client, real, fx):
    unlock(client, real)
    price(client, 100)
    fx.lose_answer = True
    r = buy(client, real).json()
    assert r["fill"] is not None and len(fx.sent) == 1


def test_clear_refusal_buys_nothing(client, real, fx):
    unlock(client, real)
    price(client, 100)
    fx.order_error = ExchangeError(ErrorKind.UNEXPECTED, "51008", "Insufficient balance")
    r = buy(client, real)
    assert r.status_code == 502 and "51008" in r.json()["error"]
    assert client.get(f"/api/live/{real}").json()["positions"] == []


def test_budget_limits_and_lock(client, real, fx):
    unlock(client, real)
    assert client.put(f"/api/live/{real}/limits", json={"per_trade": "50", "budget": "40"}).status_code == 400
    v = client.put(f"/api/live/{real}/limits", json={"per_trade": "10", "budget": "300"}).json()
    assert v["per_trade"] == "10" and v["account"]["starting_balance"] == "300.00"
    price(client, 100)
    buy(client, real)
    assert fx.sent[-1][2] * D("100.05") <= D("10")
    v = client.post(f"/api/live/{real}/lock").json()
    assert v["unlocked"] is False and len(v["positions"]) == 1  # open trades stay managed
    assert buy(client, real).status_code == 403


def test_coins_gone_from_the_exchange_are_removed(client, real, fx):
    unlock(client, real)
    price(client, 100)
    buy(client, real)
    fx.balances["BTC"] = D("0")
    assert client.post(f"/api/live/{real}/positions/BTC-USDT/close").status_code == 502
    assert client.get(f"/api/live/{real}").json()["positions"] == []
    assert any("no longer in your" in a["message"] for a in client.get("/api/alerts").json())


def test_real_auto_trade_buys_setups_with_the_real_book(client, real, fx, candle_feed):
    candle_feed.pattern = "breakout"
    price(client, 125.65, 125.70)
    price(client, 125.65, 125.70, symbol="ETH-USDT")
    assert client.put(f"/api/live/{real}/autotrade", json={"on": True}).status_code == 409  # still locked
    unlock(client, real)
    fx.fill_price = D("125.7")
    status = client.put(f"/api/live/{real}/autotrade", json={"on": True}).json()
    assert status["on"] is True
    assert {p["state"] for p in status["pairs"]} == {"bought"}, status["pairs"]
    assert {s for s, side, *_ in fx.sent if side == "buy"} == {"BTC-USDT", "ETH-USDT"}
    # The practice Auto-trade is a separate switch and stays off.
    assert client.get(f"/api/autopilot/{real}").json()["on"] is False
    # Locking turns real Auto-trade off too.
    client.post(f"/api/live/{real}/lock")
    assert client.get(f"/api/live/{real}").json()["autotrade"]["on"] is False
