"""Shared signed-request loop: rate limiting, clock resync and bounded retries."""
from __future__ import annotations

import asyncio
import json
import random
import time
from abc import abstractmethod
from typing import Any

import httpx

from .base import ErrorKind, ExchangeConnector, ExchangeError
from .ratelimit import RateLimiter

MAX_RETRIES = 3


class SignedHttpConnector(ExchangeConnector):
    base_url: str
    rate_limits: dict[str, tuple[int, float]]

    def __init__(self, credentials=None, client: httpx.AsyncClient | None = None):
        self.credentials = credentials
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        self._limiter = RateLimiter(self.rate_limits)
        self._clock_offset_ms = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- per-exchange hooks -------------------------------------------------
    @abstractmethod
    def _auth_headers(self, method: str, path: str, body: str) -> dict[str, str]: ...

    @abstractmethod
    def _unwrap(self, status: int, payload: Any) -> Any:
        """Return the data portion or raise ExchangeError."""

    @abstractmethod
    async def _server_time_ms(self) -> int: ...

    # --- shared request loop ------------------------------------------------
    async def _request(self, method: str, path: str, *, group: str, signed: bool, body: dict | None = None) -> Any:
        body_text = json.dumps(body, separators=(",", ":")) if body else ""
        resynced = False
        for attempt in range(MAX_RETRIES + 1):
            await self._limiter.acquire(group)
            headers = {"Content-Type": "application/json"}
            if signed:
                if self.credentials is None:
                    raise ValueError("signed request without credentials")
                headers.update(self._auth_headers(method, path, body_text))
            try:
                resp = await self._client.request(method, path, content=body_text or None, headers=headers)
            except httpx.TransportError as exc:
                err = ExchangeError(ErrorKind.NETWORK, detail=type(exc).__name__)
            else:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
                try:
                    return self._unwrap(resp.status_code, payload)
                except ExchangeError as exc:
                    err = exc

            if err.kind == ErrorKind.CLOCK_SKEW and not resynced:
                await self._resync_clock()
                resynced = True
                continue
            if err.kind in (ErrorKind.RATE_LIMITED, ErrorKind.EXCHANGE_UNAVAILABLE, ErrorKind.NETWORK) and attempt < MAX_RETRIES:
                await asyncio.sleep(self._backoff(attempt))
                continue
            raise err
        raise err  # pragma: no cover

    async def _resync_clock(self) -> None:
        server = await self._server_time_ms()
        self._clock_offset_ms = server - int(time.time() * 1000)

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(8.0, 0.5 * 2**attempt) * (0.5 + random.random() / 2)
