"""End-to-end portal behaviour through the HTTP API, using the fake connector."""
import logging

import pytest

from app.exchanges.base import ErrorKind, ExchangeError, Permission

SECRET = "TOPSECRETvalue987"
PASS = "MyPassphrase555"


def connect(client, key="apikey-0001abcd", exchange="okx"):
    return client.post("/api/connections", json={"exchange": exchange, "api_key": key, "api_secret": SECRET, "passphrase": PASS})


def creds_rows(client):
    store = client.app_ref.state.service.store
    return store._db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]


def assert_no_secrets(resp):
    assert SECRET not in resp.text and PASS not in resp.text


def test_requires_auth(client):
    client.headers.pop("Authorization")
    assert client.get("/api/connections").status_code == 401
    assert client.post("/api/login", json={"token": "wrong-token-xxxxxxx"}).status_code == 401
    assert client.post("/api/login", json={"token": "test-access-token-0123456789"}).status_code == 200
    assert client.get("/api/connections").status_code == 200  # cookie session


def test_wizard_content_lists_both_exchanges(client):
    r = client.get("/api/exchanges").json()
    assert {e["exchange_id"] for e in r} == {"okx", "kucoin"}
    assert all(e["server_ips"] == ["203.0.113.10"] for e in r)
    assert all("Withdraw" in " ".join(e["permissions_to_never_enable"]) for e in r)


def test_read_only_key_connects(client):
    r = connect(client)
    assert r.status_code == 200
    c = r.json()
    assert c["state"] == "CONNECTED_READONLY" and c["health"] == "ok"
    assert c["key_masked"] == "••••••••abcd"
    assert c["live_trading"] is False and c["trading_mode"] == "paper"
    assert c["issues"] == []
    assert {b["currency"] for b in c["balances"]} >= {"BTC", "USDT"}
    assert c["available_pairs"] == ["BTC-USDT", "ETH-USDT"]
    assert_no_secrets(r)
    assert creds_rows(client) == 1


def test_withdraw_key_is_rejected_and_deleted(client, fx):
    fx.permissions = {Permission.READ, Permission.WITHDRAW}
    fx.raw = ["read_only", "withdraw"]
    c = connect(client).json()
    assert c["state"] == "REJECTED"
    assert any(i["code"] == "withdraw_enabled" for i in c["issues"])
    assert creds_rows(client) == 0


def test_transfer_key_is_rejected(client, fx):
    fx.permissions = {Permission.READ, Permission.TRANSFER}
    assert connect(client).json()["state"] == "REJECTED"
    assert creds_rows(client) == 0


def test_trade_key_and_no_ip_are_warnings(client, fx):
    fx.permissions = {Permission.READ, Permission.TRADE}
    fx.ip_restricted = False
    c = connect(client).json()
    assert c["state"] == "CONNECTED_READONLY"
    assert {i["code"] for i in c["issues"]} == {"trade_enabled", "no_ip_restriction"}


def test_invalid_key_gets_plain_message(client, fx):
    fx.error = ExchangeError(ErrorKind.INVALID_PASSPHRASE, "50105")
    r = connect(client)
    c = r.json()
    assert c["state"] == "REJECTED"
    assert "passphrase is wrong" in c["last_error"]
    assert creds_rows(client) == 0
    assert_no_secrets(r)


def test_one_active_connection_per_exchange(client):
    connect(client)
    assert connect(client).status_code == 409
    assert connect(client, exchange="kucoin").status_code == 200


def test_input_validation_never_echoes_values(client):
    r = client.post("/api/connections", json={"exchange": "okx", "api_key": "k" * 300, "api_secret": SECRET, "passphrase": PASS})
    assert r.status_code == 422
    assert "k" * 300 not in r.text
    assert_no_secrets(r)


def test_paper_requires_settings_and_live_is_locked(client):
    cid = connect(client).json()["id"]
    assert client.post(f"/api/connections/{cid}/paper").status_code == 400
    assert client.put(f"/api/connections/{cid}/settings", json={"allocation_pct": 150, "pairs": ["BTC-USDT"]}).status_code == 400
    assert client.put(f"/api/connections/{cid}/settings", json={"allocation_pct": 20, "pairs": ["FAKE-USDT"]}).status_code == 400
    r = client.put(f"/api/connections/{cid}/settings", json={"allocation_pct": 20, "pairs": ["BTC-USDT"]})
    assert r.json()["selected_pairs"] == ["BTC-USDT"]
    c = client.post(f"/api/connections/{cid}/paper").json()
    assert c["state"] == "PAPER" and c["live_trading"] is False
    r = client.post(f"/api/connections/{cid}/live")
    assert r.status_code == 403 and "locked" in r.json()["error"]
    assert client.get(f"/api/connections/{cid}").json()["live_trading"] is False


def test_auth_failure_detected_and_recovered(client, fx):
    cid = connect(client).json()["id"]
    fx.error = ExchangeError(ErrorKind.INVALID_KEY, "50111")
    c = client.post(f"/api/connections/{cid}/test").json()
    assert c["health"] == "auth_failed"
    alerts = client.get("/api/alerts").json()
    assert alerts[0]["level"] == "critical" and "Authentication failed" in alerts[0]["message"]
    # repeated failures don't spam alerts
    client.post(f"/api/connections/{cid}/test")
    assert len(client.get("/api/alerts").json()) == 1
    fx.error = None
    assert client.post(f"/api/connections/{cid}/test").json()["health"] == "ok"
    assert "working again" in client.get("/api/alerts").json()[0]["message"]


def test_connectivity_interruption_is_degraded(client, fx):
    cid = connect(client).json()["id"]
    fx.error = ExchangeError(ErrorKind.NETWORK)
    assert client.post(f"/api/connections/{cid}/sync").json()["health"] == "degraded"
    assert client.get("/api/alerts").json()[0]["level"] == "warning"


def test_withdraw_added_later_stops_connection(client, fx):
    cid = connect(client).json()["id"]
    fx.permissions = {Permission.READ, Permission.WITHDRAW}
    c = client.post(f"/api/connections/{cid}/test").json()
    assert c["state"] == "REJECTED"
    assert creds_rows(client) == 0
    assert client.get("/api/alerts").json()[0]["level"] == "critical"


def test_replace_key_keeps_old_on_failure(client, fx):
    cid = connect(client, key="old-key-0000aaaa").json()["id"]
    fx.valid_keys = {"old-key-0000aaaa"}
    r = client.put(f"/api/connections/{cid}/credentials", json={"api_key": "new-key-0000bbbb", "api_secret": SECRET, "passphrase": PASS})
    assert r.status_code == 400 and "kept the old one" in r.json()["error"]
    assert client.get(f"/api/connections/{cid}").json()["key_masked"].endswith("aaaa")
    fx.valid_keys = {"old-key-0000aaaa", "new-key-0000bbbb"}
    r = client.put(f"/api/connections/{cid}/credentials", json={"api_key": "new-key-0000bbbb", "api_secret": SECRET, "passphrase": PASS})
    assert r.json()["key_masked"].endswith("bbbb")
    assert_no_secrets(r)
    # Later calls use the new decrypted key
    fx.seen_credentials.clear()
    client.post(f"/api/connections/{cid}/test")
    assert fx.seen_credentials[-1].api_key == "new-key-0000bbbb"


def test_disconnect_deletes_credentials(client):
    cid = connect(client).json()["id"]
    c = client.delete(f"/api/connections/{cid}").json()
    assert c["state"] == "DISCONNECTED" and c["balances"] == []
    assert creds_rows(client) == 0
    assert client.post(f"/api/connections/{cid}/test").status_code == 409
    actions = [a["action"] for a in client.get(f"/api/connections/{cid}/audit").json()]
    assert "disconnected" in actions and "validated" in actions


def test_secrets_never_in_any_response_or_log(client, caplog):
    caplog.set_level(logging.DEBUG)
    cid = connect(client).json()["id"]
    client.put(f"/api/connections/{cid}/settings", json={"allocation_pct": 10, "pairs": ["ETH-USDT"]})
    for r in [
        client.get("/api/connections"),
        client.get(f"/api/connections/{cid}"),
        client.get(f"/api/connections/{cid}/audit"),
        client.post(f"/api/connections/{cid}/test"),
        client.get("/api/alerts"),
    ]:
        assert_no_secrets(r)
    assert SECRET not in caplog.text and PASS not in caplog.text
    db_dump = "\n".join(client.app_ref.state.service.store._db.iterdump())
    assert SECRET not in db_dump and PASS not in db_dump


def test_security_headers(client):
    r = client.get("/api/connections")
    assert r.headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert client.get("/").status_code == 200
