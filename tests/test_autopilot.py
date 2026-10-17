"""Paper autopilot and the Home screen feed."""
import asyncio

from .test_paper_api import paper, price  # noqa: F401 - fixture


def run(client):
    return asyncio.run(client.app_ref.state.autopilot.run_once())


def setup(client, cid, candle_feed, pattern="breakout"):
    candle_feed.pattern = pattern
    client.post(f"/api/paper/{cid}/account", json={"starting_balance": "10000"})
    price(client, 125.65, 125.70)
    price(client, 125.65, 125.70, symbol="ETH-USDT")


def test_autopilot_is_off_by_default_and_does_nothing(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    assert client.get(f"/api/autopilot/{paper}").json()["on"] is False
    assert run(client) == []
    assert client.get(f"/api/paper/{paper}").json()["positions"] == []


def test_autopilot_needs_a_paper_account(client, paper):  # noqa: F811
    r = client.put(f"/api/autopilot/{paper}", json={"on": True})
    assert r.status_code == 409 and "paper account" in r.json()["error"]


def test_autopilot_buys_setups_once_per_candle(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    status = client.put(f"/api/autopilot/{paper}", json={"on": True}).json()
    assert status["on"] is True
    assert {p["state"] for p in status["pairs"]} == {"bought"}
    positions = client.get(f"/api/paper/{paper}").json()["positions"]
    assert {p["symbol"] for p in positions} == {"BTC-USDT", "ETH-USDT"}
    # Holding now, and the same candle is never acted on twice.
    assert run(client) == []
    assert {p["state"] for p in client.get(f"/api/autopilot/{paper}").json()["pairs"]} == {"holding"}
    [dec, _] = client.get(f"/api/decisions?connection_id={paper}").json()
    assert dec["inputs"]["automatic"] is True and dec["inputs"]["source"] == "strategy"
    feed = client.get("/api/home").json()["activity"]
    assert feed[0]["title"].startswith("Autopilot bought") and "breakout above" in feed[0]["detail"]


def test_autopilot_waits_without_a_setup(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed, pattern="down")
    status = client.put(f"/api/autopilot/{paper}", json={"on": True}).json()
    assert {p["state"] for p in status["pairs"]} == {"waiting"}
    assert status["pairs"][0]["text"].startswith("No buy setup yet.")
    assert client.get(f"/api/paper/{paper}").json()["positions"] == []


def test_autopilot_respects_the_emergency_stop(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    client.post("/api/risk/kill-switch", json={"on": True, "reason": "test"})
    status = client.put(f"/api/autopilot/{paper}", json={"on": True}).json()
    assert {p["state"] for p in status["pairs"]} == {"paused"}
    assert client.get(f"/api/paper/{paper}").json()["positions"] == []


def test_blocked_setup_is_tried_once_and_shown(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    cfg = client.get("/api/strategy/config").json()
    client.put("/api/strategy/config", json={**cfg, "target_atr": 4})  # costs eat the edge, so the gate says no
    status = client.put(f"/api/autopilot/{paper}", json={"on": True}).json()
    assert {p["state"] for p in status["pairs"]} == {"blocked"}
    assert run(client) == []  # same candle: not retried
    feed = client.get("/api/home").json()["activity"]
    assert feed[0]["kind"] == "blocked" and feed[0]["title"].startswith("Autopilot did not buy")


def test_home_summarises_mode_and_accounts(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    home = client.get("/api/home").json()
    assert home["mode"] == "paper" and home["live_trading"] == "locked" and home["kill_switch"] is False
    [acct] = home["accounts"]
    assert acct["exchange"] == "okx" and acct["account"]["equity"] == "10000.00"
    assert acct["autopilot"]["on"] is False and home["activity"] == []
    client.put(f"/api/autopilot/{paper}", json={"on": False})
    assert client.get(f"/api/autopilot/{paper}").json()["on"] is False


def _hold(client, cid, candle_feed):
    """Autopilot buys BTC on the breakout, then the position is backdated so later candles count as after entry."""
    setup(client, cid, candle_feed)
    client.put(f"/api/autopilot/{cid}", json={"on": True})
    trading = client.app_ref.state.paper.trading
    trading._exec("UPDATE paper_positions SET opened_at = '2000-01-01T00:00:00+00:00'")
    return trading.position(cid, "BTC-USDT")


def test_autopilot_sells_when_the_trend_breaks(client, paper, candle_feed):  # noqa: F811
    _hold(client, paper, candle_feed)
    candle_feed.pattern = "down"  # latest close is far below the fast EMA
    client.app_ref.state.paper.strategy.candles._cache.clear()
    price(client, 125, 125.05)  # still above the stop, so only the trend rule can sell
    run(client)
    assert client.get(f"/api/paper/{paper}").json()["positions"] == []
    checks = {p["symbol"]: p for p in client.get(f"/api/autopilot/{paper}").json()["pairs"]}
    assert checks["BTC-USDT"]["state"] == "sold" and "trend weakened" in checks["BTC-USDT"]["text"]
    feed = client.get("/api/home").json()["activity"]
    assert any(i["detail"].startswith("Autopilot sold") for i in feed)


def test_autopilot_raises_the_stop_but_never_lowers_it(client, paper, candle_feed):  # noqa: F811
    pos = _hold(client, paper, candle_feed)
    trading = client.app_ref.state.paper.trading
    trading.set_stop(pos["id"], pos["stop_price"] - 5)  # pretend the stop was set further down
    run(client)
    raised = trading.position(paper, "BTC-USDT")["stop_price"]
    assert raised > pos["stop_price"] - 5
    trading.set_stop(pos["id"], raised + 1)  # a higher stop is never lowered
    run(client)
    assert trading.position(paper, "BTC-USDT")["stop_price"] == raised + 1


def test_markets_are_ranked_and_chart_has_overlays(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    m = client.get(f"/api/markets/{paper}").json()
    assert m["best"] in ("BTC-USDT", "ETH-USDT") and m["markets"][0]["score"] == 100
    assert m["markets"][0]["checks"][0]["ok"] is True and m["markets"][0]["change_24h_pct"] is not None
    c = client.get(f"/api/chart/{paper}/BTC-USDT").json()
    assert len(c["candles"]) == 100 and len(c["ema_fast"]) == 100 and c["resistance"]
    assert c["position"] is None and c["signal"]["status"] == "BUY_SETUP"
    client.put(f"/api/autopilot/{paper}", json={"on": True})
    c = client.get(f"/api/chart/{paper}/BTC-USDT").json()
    assert c["position"]["stop"] < c["position"]["entry"] and c["markers"][0]["side"] == "buy"


def test_trading_style_switch(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    assert client.get(f"/api/autopilot/{paper}").json()["style"] == "careful"
    r = client.put("/api/strategy/style", json={"style": "active"}).json()
    assert r["style"] == "active" and r["config"]["volume_multiple"] == 1.1
    assert client.get(f"/api/autopilot/{paper}").json()["style"] == "active"
    cfg = client.get("/api/strategy/config").json()
    client.put("/api/strategy/config", json={**cfg, "rsi_min": 40})
    assert client.get(f"/api/autopilot/{paper}").json()["style"] == "custom"
    assert client.put("/api/strategy/style", json={"style": "yolo"}).status_code == 422


def test_stop_is_never_too_tight_for_fees():
    from app.strategy.breakout import evaluate
    from app.strategy.config import StrategyConfig
    from .candles import make_candles
    sig = evaluate("BTC-USDT", make_candles("breakout"), StrategyConfig(min_stop_pct=5))
    price = sig.metrics["price"]
    assert abs((price - sig.suggested_stop) / price - 0.05) < 1e-6
    assert abs((sig.suggested_target - price) - 3 * (price - sig.suggested_stop)) < 1e-4  # same 6:2 shape


def test_problems_explain_why_nothing_is_bought(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    client.post("/api/risk/kill-switch", json={"on": True, "reason": "test"})
    status = client.put(f"/api/autopilot/{paper}", json={"on": True}).json()
    assert any("Emergency stop" in p for p in status["problems"])


def test_a_waiting_setup_is_retried_on_the_same_candle(client, paper, candle_feed):  # noqa: F811
    setup(client, paper, candle_feed)
    ap = client.app_ref.state.autopilot
    real = ap.paper.place_order
    calls = []

    async def wait_once(req):
        calls.append(req.symbol)
        if len(calls) <= 2:
            return {"decision": {"status": "WAIT", "summary": "Market data is current: price is 11s old."}, "fill": None}
        return await real(req)

    ap.paper.place_order = wait_once
    status = client.put(f"/api/autopilot/{paper}", json={"on": True}).json()
    assert {p["state"] for p in status["pairs"]} == {"error"}
    assert "Will try again" in status["pairs"][0]["text"]
    run(client)
    assert {p["state"] for p in client.get(f"/api/autopilot/{paper}").json()["pairs"]} == {"bought"}


def test_old_news_suspensions_get_an_end_time_and_show_as_problems(client, paper, candle_feed):  # noqa: F811
    from app.paper.store import TradingStore
    setup(client, paper, candle_feed)
    trading = client.app_ref.state.paper.trading
    # A suspension saved by an older version, which never expired, plus one the user set by hand.
    trading._exec("INSERT INTO suspensions (scope, target, reason, created_at) VALUES "
                  "('asset', 'BTC', 'News: Bitcoin bridge exploited', '2026-10-13T08:00:00+00:00'), "
                  "('asset', 'ETH', 'my own pause', '2026-10-13T08:00:00+00:00')")
    TradingStore(client.app_ref.state.service.store)  # restart: the migration runs on start-up
    assert [s["target"] for s in trading.active_suspensions()] == ["ETH"]  # the old news one has expired
    status = client.put(f"/api/autopilot/{paper}", json={"on": True}).json()
    assert any("paused for ETH until you lift it" in p for p in status["problems"])
