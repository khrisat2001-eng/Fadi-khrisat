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
