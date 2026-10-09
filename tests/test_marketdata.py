"""Ticker parsing and WebSocket supervision (fake sockets, no network)."""
import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from app.marketdata.protocols import KucoinPublicProtocol, OkxPublicProtocol
from app.marketdata.supervisor import MarketDataManager, StreamSupervisor


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(StreamSupervisor, "_backoff", lambda self, attempt: 0.01)


def okx_msg(sym="BTC-USDT", bid="100", ask="100.1", last="100.05"):
    return json.dumps({"arg": {"channel": "tickers", "instId": sym},
                       "data": [{"instId": sym, "bidPx": bid, "askPx": ask, "last": last, "ts": "1700000000000"}]})


# --- parsing -----------------------------------------------------------------------
def test_okx_parse():
    p = OkxPublicProtocol()
    [t] = p.parse(okx_msg())
    assert (t.exchange, t.symbol, t.bid, t.ask) == ("okx", "BTC-USDT", Decimal("100"), Decimal("100.1"))
    assert round(t.spread_bps, 2) == Decimal("10.00")
    assert p.parse("pong") == []
    assert p.parse(json.dumps({"event": "subscribe", "arg": {"channel": "tickers"}})) == []
    assert p.parse("not json") == []
    assert p.parse(okx_msg(bid="101", ask="100")) == []  # crossed book ignored
    assert p.parse(okx_msg(bid="", ask="100")) == []


def test_okx_subscribe_batches():
    msgs = OkxPublicProtocol().subscribe_messages([f"C{i}-USDT" for i in range(120)])
    assert len(msgs) == 3
    assert json.loads(msgs[0])["args"][0] == {"channel": "tickers", "instId": "C0-USDT"}


def test_kucoin_parse():
    p = KucoinPublicProtocol()
    raw = json.dumps({"type": "message", "topic": "/market/ticker:ETH-USDT", "subject": "trade.ticker",
                      "data": {"bestBid": "2000", "bestAsk": "2000.5", "price": "2000.2", "time": 1700000000000}})
    [t] = p.parse(raw)
    assert (t.exchange, t.symbol, t.ask) == ("kucoin", "ETH-USDT", Decimal("2000.5"))
    assert p.parse(json.dumps({"type": "pong"})) == []
    assert p.parse(json.dumps({"type": "welcome"})) == []


async def test_kucoin_endpoint_uses_token_and_ping_interval():
    def handler(req):
        assert req.url.path == "/api/v1/bullet-public"
        return httpx.Response(200, json={"code": "200000", "data": {
            "token": "tok123", "instanceServers": [{"endpoint": "wss://ws-api-spot.kucoin.com/", "pingInterval": 18000, "pingTimeout": 10000}]}})

    client = httpx.AsyncClient(base_url=KucoinPublicProtocol.rest_base, transport=httpx.MockTransport(handler))
    p = KucoinPublicProtocol(client)
    url = await p.endpoint()
    assert url.startswith("wss://ws-api-spot.kucoin.com/?token=tok123&connectId=")
    assert p.ping_interval == pytest.approx(14.4)


# --- supervision -------------------------------------------------------------------
class ScriptedSocket:
    def __init__(self, messages, then="close"):
        self.messages = list(messages)
        self.then = then
        self.sent = []
        self.closed = False
        self._closed = asyncio.Event()

    async def send(self, m):
        self.sent.append(m)

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.then == "close":
            raise ConnectionError("server closed")
        await self._closed.wait()  # like a real socket: recv fails once closed
        raise ConnectionError("closed")

    async def close(self):
        self.closed = True
        self._closed.set()


async def wait_for(cond, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_reconnects_and_resubscribes_with_alerts():
    sockets = [ScriptedSocket([okx_msg(last="1")]), ScriptedSocket([okx_msg(last="2")], then="hang")]
    opened = []

    async def connect(url):
        s = sockets.pop(0)
        opened.append(s)
        return s

    alerts = []
    mgr = MarketDataManager(connect, on_alert=lambda level, msg: alerts.append((level, msg)))
    await mgr.reconcile({"okx": {"BTC-USDT"}})
    await wait_for(lambda: mgr.latest("okx", "BTC-USDT") and mgr.latest("okx", "BTC-USDT").last == Decimal("2"))
    await wait_for(lambda: mgr.stream_state("okx") == "connected")
    assert len(opened) == 2
    for s in opened:  # every connection resubscribed
        assert json.loads(s.sent[0])["op"] == "subscribe"
    assert mgr.status()[0]["reconnects"] == 1
    assert ("warning", "OKX market data stream disconnected. Reconnecting.") in alerts
    assert ("info", "OKX market data stream reconnected.") in alerts
    await mgr.stop_all()
    assert mgr.stream_state("okx") == "stopped"


async def test_silent_stream_is_pinged_then_dropped():
    first = ScriptedSocket([], then="hang")
    second = ScriptedSocket([], then="hang")
    queue = [first, second]

    async def connect(url):
        return queue.pop(0)

    proto = OkxPublicProtocol()
    proto.ping_interval = 0.05
    sup = StreamSupervisor(proto, ["BTC-USDT"], lambda t: None, connect=connect)
    sup.start()
    await wait_for(lambda: first.closed)
    assert "ping" in first.sent
    await wait_for(lambda: sup.status.state == "connected" and not queue)
    await sup.stop()


async def test_symbol_change_resubscribes():
    socks = []

    async def connect(url):
        s = ScriptedSocket([], then="hang")
        socks.append(s)
        return s

    mgr = MarketDataManager(connect)
    await mgr.reconcile({"okx": {"BTC-USDT"}})
    await wait_for(lambda: len(socks) == 1 and socks[0].sent)
    await mgr.reconcile({"okx": {"BTC-USDT", "ETH-USDT"}})
    await wait_for(lambda: len(socks) == 2 and socks[1].sent)
    args = json.loads(socks[1].sent[0])["args"]
    assert {a["instId"] for a in args} == {"BTC-USDT", "ETH-USDT"}
    await mgr.reconcile({})
    assert mgr.status() == []


async def test_listener_errors_do_not_stop_feed():
    async def connect(url):
        return ScriptedSocket([okx_msg(last="1"), okx_msg(last="2")], then="hang")

    mgr = MarketDataManager(connect)
    mgr.add_listener(lambda t: 1 / 0)
    await mgr.reconcile({"okx": {"BTC-USDT"}})
    await wait_for(lambda: mgr.latest("okx", "BTC-USDT") and mgr.latest("okx", "BTC-USDT").last == Decimal("2"))
    await mgr.stop_all()
