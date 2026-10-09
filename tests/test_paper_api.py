"""Paper trading end to end through the API, with injected live prices."""
import time
from decimal import Decimal as D

import pytest

from app.marketdata.models import Ticker
from app.risk.gate import TradeRequest

from .test_portal_api import connect


def wait(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def price(client, bid, ask=None, symbol="BTC-USDT", exchange="okx"):
    bid = D(str(bid))
    ask = D(str(ask)) if ask is not None else bid * D("1.0005")
    client.app_ref.state.market._on_ticker(Ticker(exchange, symbol, bid, ask, bid, 0))


@pytest.fixture
def paper(client):
    cid = connect(client).json()["id"]
    client.put(f"/api/connections/{cid}/settings", json={"allocation_pct": 20, "pairs": ["BTC-USDT", "ETH-USDT"]})
    client.post(f"/api/connections/{cid}/paper")
    wait(lambda: client.app_ref.state.market.stream_state("okx") == "connected")
    return cid


def buy(client, cid, stop="98", tp="106", symbol="BTC-USDT"):
    return client.post(f"/api/paper/{cid}/orders", json={"symbol": symbol, "stop_price": stop, "take_profit_price": tp}).json()


def test_streams_follow_selected_pairs(client, paper, sockets):
    status = client.get("/api/market").json()["streams"]
    assert status[0]["exchange"] == "okx" and set(status[0]["symbols"]) == {"BTC-USDT", "ETH-USDT"}
    assert '"instId": "BTC-USDT"' in sockets.opened[-1].sent[0]
    client.delete(f"/api/connections/{paper}")
    assert client.get("/api/market").json()["streams"] == []


def test_account_defaults_to_allocation_of_usdt(client, paper):
    acct = client.post(f"/api/paper/{paper}/account", json={}).json()["account"]
    assert acct["starting_balance"] == "200.00"  # 20% of the fake 1,000 USDT
    assert client.post(f"/api/paper/{paper}/account", json={}).status_code == 409


def test_account_needs_paper_mode(client):
    cid = connect(client).json()["id"]
    assert client.post(f"/api/paper/{cid}/account", json={}).status_code == 409


def test_approved_order_fills_and_is_logged(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    r = buy(client, paper)
    assert r["decision"]["status"] == "APPROVE", r["decision"]["summary"]
    fill = r["fill"]
    assert D(fill["price"]) > D("100.05")  # ask plus slippage
    view = client.get(f"/api/paper/{paper}").json()
    [pos] = view["positions"]
    assert pos["symbol"] == "BTC-USDT" and D(pos["qty"]) == D(fill["qty"])
    assert D(view["account"]["cash"]) < D("10000")
    [dec] = client.get(f"/api/decisions?connection_id={paper}").json()
    assert dec["status"] == "APPROVE" and len(dec["checks"]) >= 10
    assert dec["inputs"]["bid"] == "100"


def test_no_price_means_wait_and_no_fill(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    r = buy(client, paper)
    assert r["decision"]["status"] == "WAIT" and r["fill"] is None
    assert client.get(f"/api/paper/{paper}").json()["positions"] == []


def test_stop_loss_and_take_profit_trigger(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    buy(client, paper)
    price(client, 99)  # above stop: nothing happens
    assert len(client.get(f"/api/paper/{paper}").json()["positions"]) == 1
    price(client, 97.5)
    view = client.get(f"/api/paper/{paper}").json()
    assert view["positions"] == []
    assert view["fills"][0]["reason"] == "stop_loss" and D(view["fills"][0]["realized_pnl"]) < 0
    assert "Stop-loss hit" in client.get("/api/alerts").json()[0]["message"]

    price(client, 100)
    buy(client, paper)
    price(client, 106.5)
    view = client.get(f"/api/paper/{paper}").json()
    assert view["positions"] == [] and view["fills"][0]["reason"] == "take_profit"
    assert D(view["fills"][0]["realized_pnl"]) > 0


def test_stops_can_tighten_but_never_widen(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    buy(client, paper)
    r = client.put(f"/api/paper/{paper}/positions/BTC-USDT/stop", json={"stop_price": "97"})
    assert r.status_code == 400 and "widening" in r.json()["error"]
    assert client.put(f"/api/paper/{paper}/positions/BTC-USDT/stop", json={"stop_price": "101"}).status_code == 400  # above bid
    view = client.put(f"/api/paper/{paper}/positions/BTC-USDT/stop", json={"stop_price": "99"}).json()
    assert view["positions"][0]["stop_price"] == "99"
    # Adding to the position with a looser stop keeps the tighter one.
    price(client, 100)
    buy(client, paper, stop="97", tp="110")
    assert client.get(f"/api/paper/{paper}").json()["positions"][0]["stop_price"] == "99"


def test_manual_close(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    buy(client, paper)
    fill = client.post(f"/api/paper/{paper}/positions/BTC-USDT/close").json()
    assert fill["reason"] == "manual_close"
    assert client.post(f"/api/paper/{paper}/positions/BTC-USDT/close").status_code == 404


def test_kill_switch_blocks_entries_but_stops_still_work(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    buy(client, paper)
    assert client.post("/api/risk/kill-switch", json={"on": True, "reason": "test"}).json() == {"on": True}
    price(client, 100, symbol="ETH-USDT")
    r = buy(client, paper, symbol="ETH-USDT")
    assert r["decision"]["status"] == "REJECT" and r["fill"] is None
    price(client, 97)
    assert client.get(f"/api/paper/{paper}").json()["positions"] == []  # protective stop still executed


def test_suspensions(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    [s] = client.post("/api/risk/suspensions", json={"scope": "asset", "target": "btc", "reason": "exploit under review"}).json()
    assert s["target"] == "BTC"
    r = buy(client, paper)
    assert r["decision"]["status"] == "REJECT" and "exploit under review" in r["decision"]["summary"]
    assert client.delete(f"/api/risk/suspensions/{s['id']}").json() == []
    assert buy(client, paper)["decision"]["status"] == "APPROVE"


def test_risk_config_round_trip_and_bounds(client):
    cfg = client.get("/api/risk/config").json()
    cfg["max_risk_per_trade_pct"] = "0.5"
    assert client.put("/api/risk/config", json=cfg).json()["max_risk_per_trade_pct"] == "0.5"
    cfg["max_risk_per_trade_pct"] = "50"
    assert client.put("/api/risk/config", json=cfg).status_code == 422


def test_execution_requires_gate_token(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    svc = client.app_ref.state.paper
    req = TradeRequest(paper, "BTC-USDT", "buy", D("98"), D("106"))
    with pytest.raises(PermissionError):
        svc.execute(None, req, D("1"), svc.risk_config())
    with pytest.raises(PermissionError):
        svc.execute("forged|token", req, D("1"), svc.risk_config())
    assert client.get(f"/api/paper/{paper}").json()["positions"] == []


def test_disconnect_removes_paper_account(client, paper):
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    client.delete(f"/api/connections/{paper}")
    assert client.get(f"/api/paper/{paper}").json()["account"] is None


# --- strategy integration ------------------------------------------------------------
def test_signals_endpoint(client, paper, candle_feed):
    candle_feed.pattern = "breakout"
    sigs = client.get(f"/api/strategy/{paper}/signals").json()
    assert {s["symbol"] for s in sigs} == {"BTC-USDT", "ETH-USDT"}
    assert all(s["status"] == "BUY_SETUP" for s in sigs)


def test_costs_can_eat_a_strategy_edge(client, paper, candle_feed):
    # 2 ATR stop / 4 ATR target on a quiet market: gross 2:1, but fees and
    # slippage take it below the 1.5 net minimum, so the gate says no.
    candle_feed.pattern = "breakout"
    cfg = client.get("/api/strategy/config").json()
    client.put("/api/strategy/config", json={**cfg, "target_atr": 4})
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 125.65, 125.70)
    r = client.post(f"/api/paper/{paper}/orders", json={"symbol": "BTC-USDT", "source": "strategy"}).json()
    assert r["decision"]["status"] == "REJECT" and "Net reward:risk" in r["decision"]["summary"]


def test_strategy_order_uses_suggested_stop_and_target(client, paper, candle_feed):
    candle_feed.pattern = "breakout"
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 125.65, 125.70)
    r = client.post(f"/api/paper/{paper}/orders", json={"symbol": "BTC-USDT", "source": "strategy"}).json()
    assert r["decision"]["status"] == "APPROVE", r["decision"]["summary"]
    pos = client.get(f"/api/paper/{paper}").json()["positions"][0]
    assert D(pos["stop_price"]) < D("125") < D("129") < D(pos["take_profit"])
    inputs = client.get(f"/api/decisions?connection_id={paper}").json()[0]["inputs"]
    assert inputs["technical_signal"]["status"] == "BUY_SETUP"


def test_strategy_order_without_setup_is_rejected(client, paper, candle_feed):
    candle_feed.pattern = "down"
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    r = client.post(f"/api/paper/{paper}/orders", json={"symbol": "BTC-USDT", "source": "strategy"})
    body = r.json()
    assert r.status_code == 200 and body["decision"]["status"] == "REJECT" and body["fill"] is None


def test_chasing_is_blocked_even_for_manual_orders(client, paper, candle_feed):
    candle_feed.pattern = "breakout"
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 130, 130.05)  # far above the ~125.3 breakout level
    r = buy(client, paper, stop="127", tp="140")
    assert r["decision"]["status"] == "REJECT"
    assert any(c["name"] == "extension" and c["result"] == "fail" for c in r["decision"]["checks"])


def test_candle_outage_means_wait(client, paper, candle_feed):
    from app.exchanges.base import ErrorKind, ExchangeError
    candle_feed.error = ExchangeError(ErrorKind.EXCHANGE_UNAVAILABLE)
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    assert buy(client, paper)["decision"]["status"] == "WAIT"


def test_strategy_config_round_trip(client):
    cfg = client.get("/api/strategy/config").json()
    assert cfg["timeframe"] == "15m"
    cfg["timeframe"] = "1h"
    assert client.put("/api/strategy/config", json=cfg).json()["timeframe"] == "1h"
    cfg["ema_fast"] = 100
    assert client.put("/api/strategy/config", json=cfg).status_code == 422
