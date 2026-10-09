"""WebSocket supervision: heartbeats, silence detection, reconnect with backoff.

One StreamSupervisor per exchange. MarketDataManager owns the supervisors,
keeps the latest ticker per (exchange, symbol) and answers freshness queries.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .models import Ticker
from .protocols import PROTOCOLS, StreamProtocol

log = logging.getLogger("app.marketdata")


class Socket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


Connect = Callable[[str], Awaitable[Socket]]


async def websockets_connect(url: str) -> Socket:
    import websockets

    # Heartbeats are handled by the supervisor using each exchange's own ping format.
    return await websockets.connect(url, ping_interval=None, open_timeout=10, max_size=2**22)


@dataclass
class StreamStatus:
    exchange: str
    state: str = "stopped"  # stopped | connecting | connected | reconnecting
    symbols: tuple[str, ...] = ()
    connected_since: float | None = None
    last_message_at: float | None = None
    reconnects: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "state": self.state,
            "symbols": list(self.symbols),
            "connected_since": self.connected_since,
            "last_message_at": self.last_message_at,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


class StreamSupervisor:
    MAX_BACKOFF = 60.0

    def __init__(
        self,
        protocol: StreamProtocol,
        symbols: list[str],
        on_ticker: Callable[[Ticker], None],
        on_state_change: Callable[[StreamStatus, str], None] | None = None,
        connect: Connect = websockets_connect,
    ):
        self.protocol = protocol
        self.on_ticker = on_ticker
        self.on_state_change = on_state_change or (lambda status, previous: None)
        self.connect = connect
        self.status = StreamStatus(protocol.exchange_id, symbols=tuple(sorted(symbols)))
        self._socket: Socket | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    def _set_state(self, state: str, error: str | None = None) -> None:
        previous = self.status.state
        self.status.state = state
        if error is not None:
            self.status.last_error = error
        if previous != state:
            self.on_state_change(self.status, previous)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        await self._close_socket()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._set_state("stopped")

    async def set_symbols(self, symbols: list[str]) -> None:
        new = tuple(sorted(symbols))
        if new == self.status.symbols:
            return
        self.status.symbols = new
        await self._close_socket()  # the run loop reconnects and resubscribes

    async def _close_socket(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            with contextlib.suppress(Exception):
                await sock.close()

    def _backoff(self, attempt: int) -> float:
        return min(self.MAX_BACKOFF, 2 ** attempt) * (0.5 + random.random() / 2)

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            if not self.status.symbols:
                await asyncio.sleep(1)
                continue
            self._set_state("connecting" if self.status.reconnects == 0 and attempt == 0 else "reconnecting")
            got_data = False
            try:
                url = await self.protocol.endpoint()
                self._socket = await self.connect(url)
                for msg in self.protocol.subscribe_messages(list(self.status.symbols)):
                    await self._socket.send(msg)
                self.status.connected_since = time.time()
                self._set_state("connected")
                got_data = await self._read_loop(self._socket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("%s stream error: %s", self.protocol.exchange_id, type(exc).__name__)
            finally:
                await self._close_socket()
                self.status.connected_since = None
            if self._stopping:
                break
            self.status.reconnects += 1
            attempt = 0 if got_data else attempt + 1
            self._set_state("reconnecting")
            await asyncio.sleep(self._backoff(attempt))

    async def _read_loop(self, sock: Socket) -> bool:
        """Read until the socket fails or goes silent. Returns True if data arrived."""
        got_data = False
        last_heard = time.monotonic()
        interval = self.protocol.ping_interval
        while True:
            try:
                raw = await asyncio.wait_for(sock.recv(), timeout=interval)
            except asyncio.TimeoutError:
                if time.monotonic() - last_heard > interval * 2.5:
                    raise ConnectionError("no data or pong from exchange")
                await sock.send(self.protocol.ping_message())
                continue
            last_heard = time.monotonic()
            self.status.last_message_at = time.time()
            for ticker in self.protocol.parse(raw if isinstance(raw, str) else raw.decode()):
                got_data = True
                self.on_ticker(ticker)


class MarketDataManager:
    def __init__(self, connect: Connect = websockets_connect, protocol_factory=None,
                 on_alert: Callable[[str, str], None] | None = None):
        self._connect = connect
        self._protocol_factory = protocol_factory or (lambda exchange_id: PROTOCOLS[exchange_id]())
        self._on_alert = on_alert or (lambda level, message: None)
        self._supervisors: dict[str, StreamSupervisor] = {}
        self._tickers: dict[tuple[str, str], Ticker] = {}
        self._listeners: list[Callable[[Ticker], None]] = []

    def add_listener(self, fn: Callable[[Ticker], None]) -> None:
        self._listeners.append(fn)

    def _on_ticker(self, ticker: Ticker) -> None:
        self._tickers[(ticker.exchange, ticker.symbol)] = ticker
        for fn in self._listeners:
            try:
                fn(ticker)
            except Exception as exc:  # a listener bug must not stop the feed
                log.error("ticker listener error: %s", type(exc).__name__)

    def _on_state(self, status: StreamStatus, previous: str) -> None:
        name = status.exchange.upper()
        if previous == "connected" and status.state == "reconnecting":
            self._on_alert("warning", f"{name} market data stream disconnected. Reconnecting.")
        elif previous == "reconnecting" and status.state == "connected":
            self._on_alert("info", f"{name} market data stream reconnected.")

    async def reconcile(self, desired: dict[str, set[str]]) -> None:
        """Start, update or stop streams so they match the wanted symbols per exchange."""
        for exchange_id, symbols in desired.items():
            if not symbols:
                continue
            sup = self._supervisors.get(exchange_id)
            if sup is None:
                sup = StreamSupervisor(self._protocol_factory(exchange_id), sorted(symbols),
                                       self._on_ticker, self._on_state, self._connect)
                self._supervisors[exchange_id] = sup
                sup.start()
            else:
                await sup.set_symbols(sorted(symbols))
        for exchange_id in list(self._supervisors):
            if not desired.get(exchange_id):
                await self._supervisors.pop(exchange_id).stop()

    async def stop_all(self) -> None:
        await self.reconcile({})

    def latest(self, exchange: str, symbol: str) -> Ticker | None:
        return self._tickers.get((exchange, symbol))

    def stream_state(self, exchange: str) -> str:
        sup = self._supervisors.get(exchange)
        return sup.status.state if sup else "stopped"

    def status(self) -> list[dict]:
        return [s.status.as_dict() for s in self._supervisors.values()]

    def tickers(self, exchange: str | None = None) -> list[Ticker]:
        return [t for (ex, _), t in self._tickers.items() if exchange is None or ex == exchange]
