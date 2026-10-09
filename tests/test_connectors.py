"""OKX and KuCoin adapters against recorded-shape responses (no network)."""
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app.exchanges.base import Credentials, ErrorKind, ExchangeError, Permission
from app.exchanges.http import SignedHttpConnector
from app.exchanges.kucoin import KucoinConnector
from app.exchanges.okx import OkxConnector

CREDS = Credentials("key-abcd1234", "s3cret-value", "pass-phrase")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(SignedHttpConnector, "_backoff", staticmethod(lambda attempt: 0))


def make(cls, handler):
    client = httpx.AsyncClient(base_url=cls.base_url, transport=httpx.MockTransport(handler))
    return cls(CREDS, client=client)


def b64hmac(secret, msg):
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


# --- OKX -------------------------------------------------------------------
async def test_okx_signature_and_key_info():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"code": "0", "data": [{"perm": "read_only", "ip": "203.0.113.10", "uid": "42"}]})

    c = make(OkxConnector, handler)
    info = await c.get_key_info()
    assert info.permissions == {Permission.READ}
    assert info.ip_restricted is True
    assert info.account_id == "42"
    r = seen[0]
    ts = r.headers["OK-ACCESS-TIMESTAMP"]
    assert ts.endswith("Z") and len(ts) == 24
    assert r.headers["OK-ACCESS-SIGN"] == b64hmac(CREDS.api_secret, f"{ts}GET/api/v5/account/config")
    assert r.headers["OK-ACCESS-PASSPHRASE"] == CREDS.passphrase
    assert r.headers["OK-ACCESS-KEY"] == CREDS.api_key


async def test_okx_detects_withdraw_and_missing_ip():
    c = make(OkxConnector, lambda r: httpx.Response(200, json={"code": "0", "data": [{"perm": "read_only,trade,withdraw", "ip": ""}]}))
    info = await c.get_key_info()
    assert info.permissions == {Permission.READ, Permission.TRADE, Permission.WITHDRAW}
    assert info.ip_restricted is False


async def test_okx_balances_and_pairs():
    def handler(req):
        if req.url.path == "/api/v5/account/balance":
            return httpx.Response(200, json={"code": "0", "data": [{"details": [{"ccy": "BTC", "cashBal": "1.5", "availBal": "1.2"}]}]})
        assert req.url.params["instType"] == "SPOT"
        assert "OK-ACCESS-KEY" not in req.headers  # public endpoint is unsigned
        return httpx.Response(200, json={"code": "0", "data": [
            {"instId": "BTC-USDT", "baseCcy": "BTC", "quoteCcy": "USDT", "minSz": "0.00001", "tickSz": "0.1", "state": "live"},
            {"instId": "OLD-USDT", "baseCcy": "OLD", "quoteCcy": "USDT", "state": "suspend"},
        ]})

    c = make(OkxConnector, handler)
    bal = await c.get_balances()
    assert (bal[0].currency, bal[0].total, bal[0].available) == ("BTC", "1.5", "1.2")
    pairs = await c.get_trading_pairs()
    assert [p.symbol for p in pairs] == ["BTC-USDT"]


@pytest.mark.parametrize("code,kind", [
    ("50111", ErrorKind.INVALID_KEY),
    ("50113", ErrorKind.INVALID_SIGNATURE),
    ("50105", ErrorKind.INVALID_PASSPHRASE),
    ("50110", ErrorKind.IP_NOT_ALLOWED),
])
async def test_okx_auth_errors(code, kind):
    c = make(OkxConnector, lambda r: httpx.Response(401, json={"code": code, "msg": "x"}))
    with pytest.raises(ExchangeError) as e:
        await c.get_key_info()
    assert e.value.kind == kind and e.value.is_auth_failure
    assert "s3cret" not in e.value.plain_message


async def test_okx_clock_skew_resyncs_once():
    calls = {"config": 0}

    def handler(req):
        if req.url.path == "/api/v5/public/time":
            return httpx.Response(200, json={"code": "0", "data": [{"ts": "1700000000000"}]})
        calls["config"] += 1
        if calls["config"] == 1:
            return httpx.Response(401, json={"code": "50102", "msg": "Timestamp request expired"})
        return httpx.Response(200, json={"code": "0", "data": [{"perm": "read_only", "ip": "1.2.3.4"}]})

    c = make(OkxConnector, handler)
    await c.get_key_info()
    assert calls["config"] == 2
    assert c._clock_offset_ms != 0


async def test_okx_rate_limit_retries_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"code": "50011", "msg": "Too Many Requests"})
        return httpx.Response(200, json={"code": "0", "data": [{"perm": "read_only", "ip": ""}]})

    await make(OkxConnector, handler).get_key_info()
    assert len(calls) == 3


async def test_okx_rate_limit_gives_up():
    c = make(OkxConnector, lambda r: httpx.Response(429, json={"code": "50011"}))
    with pytest.raises(ExchangeError) as e:
        await c.get_key_info()
    assert e.value.kind == ErrorKind.RATE_LIMITED


async def test_network_error_maps_to_plain_error():
    def handler(req):
        raise httpx.ConnectError("boom")

    with pytest.raises(ExchangeError) as e:
        await make(OkxConnector, handler).get_balances()
    assert e.value.kind == ErrorKind.NETWORK


# --- KuCoin ----------------------------------------------------------------
async def test_kucoin_signature_and_permissions():
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"code": "200000", "data": []})
        return httpx.Response(200, json={"code": "200000", "data": {"permission": "General,Spot", "ipWhitelist": "", "uid": 7}})

    info = await make(KucoinConnector, handler).get_key_info()
    assert info.permissions == {Permission.READ, Permission.TRADE}
    assert info.ip_restricted is False
    r = seen[0]
    ts = r.headers["KC-API-TIMESTAMP"]
    assert r.headers["KC-API-SIGN"] == b64hmac(CREDS.api_secret, f"{ts}GET/api/v1/accounts")
    assert r.headers["KC-API-PASSPHRASE"] == b64hmac(CREDS.api_secret, CREDS.passphrase)
    assert r.headers["KC-API-KEY-VERSION"] == "2"


@pytest.mark.parametrize("perm,expected", [
    ("General,Withdrawal", Permission.WITHDRAW),
    ("General,Transfer", Permission.TRANSFER),
    ("General,FlexTransfers", Permission.TRANSFER),
    ("General,SomethingNew", Permission.UNKNOWN),
])
async def test_kucoin_dangerous_permissions(perm, expected):
    def handler(req):
        if req.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"code": "200000", "data": []})
        return httpx.Response(200, json={"code": "200000", "data": {"permission": perm, "ipWhitelist": "1.2.3.4"}})

    info = await make(KucoinConnector, handler).get_key_info()
    assert expected in info.permissions


async def test_kucoin_permission_list_unavailable_reports_unknown():
    def handler(req):
        if req.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"code": "200000", "data": []})
        return httpx.Response(403, json={"code": "400007", "msg": "Access denied"})

    info = await make(KucoinConnector, handler).get_key_info()
    assert info.permissions == {Permission.UNKNOWN}


async def test_kucoin_bad_passphrase():
    c = make(KucoinConnector, lambda r: httpx.Response(401, json={"code": "400004", "msg": "Invalid KC-API-PASSPHRASE"}))
    with pytest.raises(ExchangeError) as e:
        await c.get_key_info()
    assert e.value.kind == ErrorKind.INVALID_PASSPHRASE


async def test_kucoin_balances_aggregate_account_types():
    def handler(req):
        return httpx.Response(200, json={"code": "200000", "data": [
            {"currency": "USDT", "type": "main", "balance": "10.5", "available": "10.5"},
            {"currency": "USDT", "type": "trade", "balance": "4.5", "available": "2"},
            {"currency": "BTC", "type": "trade", "balance": "0.1", "available": "0.1"},
        ]})

    bal = {b.currency: b for b in await make(KucoinConnector, handler).get_balances()}
    assert bal["USDT"].total == "15.0" and bal["USDT"].available == "12.5"


async def test_kucoin_pairs_only_enabled():
    def handler(req):
        return httpx.Response(200, json={"code": "200000", "data": [
            {"symbol": "BTC-USDT", "baseCurrency": "BTC", "quoteCurrency": "USDT", "enableTrading": True},
            {"symbol": "X-USDT", "baseCurrency": "X", "quoteCurrency": "USDT", "enableTrading": False},
        ]})

    assert [p.symbol for p in await make(KucoinConnector, handler).get_trading_pairs()] == ["BTC-USDT"]


def test_credentials_repr_hides_secrets():
    text = repr(CREDS) + str(CREDS)
    assert "s3cret" not in text and "pass-phrase" not in text
