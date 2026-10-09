"""Public WebSocket protocols for OKX and KuCoin ticker streams.

Each protocol knows how to find its endpoint, build subscribe and ping messages,
and turn raw messages into Tickers. The supervisor handles connections,
heartbeats and reconnects the same way for every exchange.
"""
from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .models import Ticker


class StreamProtocol(ABC):
    exchange_id: str
    ping_interval: float = 20.0

    @abstractmethod
    async def endpoint(self) -> str:
        """URL to connect to. May need a REST call first (KuCoin)."""

    @abstractmethod
    def subscribe_messages(self, symbols: list[str]) -> list[str]: ...

    @abstractmethod
    def ping_message(self) -> str: ...

    @abstractmethod
    def parse(self, raw: str) -> list[Ticker]:
        """Return tickers in the message; [] for acks, pongs and unknown messages."""


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError):
        return None
    return d if d.is_finite() and d > 0 else None


class OkxPublicProtocol(StreamProtocol):
    exchange_id = "okx"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    # OKX closes connections idle for 30s; ping well before that.
    ping_interval = 20.0

    async def endpoint(self) -> str:
        return self.url

    def subscribe_messages(self, symbols: list[str]) -> list[str]:
        args = [{"channel": "tickers", "instId": s} for s in symbols]
        return [json.dumps({"op": "subscribe", "args": args[i:i + 50]}) for i in range(0, len(args), 50)]

    def ping_message(self) -> str:
        return "ping"

    def parse(self, raw: str) -> list[Ticker]:
        if raw == "pong":
            return []
        try:
            msg = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(msg, dict) or msg.get("arg", {}).get("channel") != "tickers":
            return []
        out = []
        for d in msg.get("data") or []:
            bid, ask, last = _dec(d.get("bidPx")), _dec(d.get("askPx")), _dec(d.get("last"))
            if bid and ask and last and bid <= ask:
                out.append(Ticker("okx", d["instId"], bid, ask, last, int(d.get("ts") or 0)))
        return out


class KucoinPublicProtocol(StreamProtocol):
    exchange_id = "kucoin"
    rest_base = "https://api.kucoin.com"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    async def endpoint(self) -> str:
        # KuCoin hands out a short-lived token and server list over REST.
        client = self._client or httpx.AsyncClient(base_url=self.rest_base, timeout=10.0)
        try:
            resp = await client.post("/api/v1/bullet-public")
            body = resp.json()
        finally:
            if self._client is None:
                await client.aclose()
        if body.get("code") != "200000":
            raise ConnectionError(f"KuCoin token request failed: {body.get('code')}")
        data = body["data"]
        server = data["instanceServers"][0]
        self.ping_interval = max(5.0, int(server.get("pingInterval", 18000)) / 1000 * 0.8)
        return f"{server['endpoint']}?token={data['token']}&connectId={uuid.uuid4().hex}"

    def subscribe_messages(self, symbols: list[str]) -> list[str]:
        msgs = []
        for i in range(0, len(symbols), 100):
            topic = "/market/ticker:" + ",".join(symbols[i:i + 100])
            msgs.append(json.dumps({"id": uuid.uuid4().hex, "type": "subscribe", "topic": topic, "privateChannel": False, "response": True}))
        return msgs

    def ping_message(self) -> str:
        return json.dumps({"id": uuid.uuid4().hex, "type": "ping"})

    def parse(self, raw: str) -> list[Ticker]:
        try:
            msg = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(msg, dict) or msg.get("type") != "message":
            return []
        topic = str(msg.get("topic", ""))
        if not topic.startswith("/market/ticker:"):
            return []
        d = msg.get("data") or {}
        symbol = topic.split(":", 1)[1]
        bid, ask, last = _dec(d.get("bestBid")), _dec(d.get("bestAsk")), _dec(d.get("price"))
        if not (bid and ask and last and bid <= ask) or "," in symbol:
            return []
        return [Ticker("kucoin", symbol, bid, ask, last, int(d.get("time") or 0))]


PROTOCOLS: dict[str, type[StreamProtocol]] = {
    OkxPublicProtocol.exchange_id: OkxPublicProtocol,
    KucoinPublicProtocol.exchange_id: KucoinPublicProtocol,
}
