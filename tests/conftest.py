import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.news.classifier import KeywordClassifier
from app.security.vault import LocalKeyProvider

from .candles import make_candles
from .fakes import FakeExchange

TOKEN = "test-access-token-0123456789"


class IdleSocket:
    """Fake WebSocket that connects fine and then stays quiet. Tests push tickers directly."""

    def __init__(self, url):
        self.url = url
        self.sent = []
        self._closed = asyncio.Event()

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):
        await self._closed.wait()
        raise ConnectionError("closed")

    async def close(self):
        self._closed.set()


class FakeSockets:
    def __init__(self):
        self.opened = []

    async def connect(self, url):
        sock = IdleSocket(url)
        self.opened.append(sock)
        return sock


class CandleFeed:
    def __init__(self):
        self.pattern = "flat"
        self.error = None
        self.calls = 0

    async def fetch(self, exchange, symbol, timeframe, limit):
        self.calls += 1
        if self.error:
            raise self.error
        return make_candles(self.pattern)[-limit:]


class NewsFeed:
    """Fake fetcher: serves bytes per source id; anything else is 'offline'."""

    def __init__(self):
        self.payloads = {}

    async def fetch(self, source):
        data = self.payloads.get(source["id"])
        if data is None:
            raise ConnectionError("offline in tests")
        if isinstance(data, Exception):
            raise data
        return data


@pytest.fixture
def news_feed():
    return NewsFeed()


@pytest.fixture
def candle_feed():
    return CandleFeed()


@pytest.fixture
def fx():
    return FakeExchange()


@pytest.fixture
def sockets():
    return FakeSockets()


@pytest.fixture
def client(fx, sockets, candle_feed, news_feed, tmp_path, monkeypatch):
    # KuCoin needs a REST token before connecting; skip that in tests.
    from app.marketdata import protocols

    async def fake_endpoint(self):
        return "wss://kucoin.test/endpoint"

    monkeypatch.setattr(protocols.KucoinPublicProtocol, "endpoint", fake_endpoint)
    settings = Settings(access_token=TOKEN, database_path=str(tmp_path / "t.db"), egress_ips=["203.0.113.10"], secure_cookies=False)
    app = create_app(settings, LocalKeyProvider(os.urandom(32)), fx.factory, run_health_monitor=False,
                     market_connect=sockets.connect, candle_fetcher=candle_feed.fetch,
                     news_fetcher=news_feed.fetch, news_classifier=KeywordClassifier())
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        c.app_ref = app
        yield c
