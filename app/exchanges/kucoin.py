"""KuCoin (spot REST API, key version 2) connector: account reads and spot market orders.

Signing: base64(HMAC-SHA256(secret, timestamp_ms + METHOD + endpoint + body)).
For key version 2 the passphrase header is base64(HMAC-SHA256(secret, passphrase)).
Docs: https://www.kucoin.com/docs-new
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any

from .base import (
    TIMEFRAME_SECONDS,
    Balance,
    Candle,
    CredentialField,
    ErrorKind,
    ExchangeError,
    KeyInfo,
    OrderResult,
    Permission,
    TradingPair,
    WizardGuide,
)
from .http import SignedHttpConnector

KUCOIN_ERRORS = {
    "400002": ErrorKind.CLOCK_SKEW,
    "400003": ErrorKind.INVALID_KEY,
    "400004": ErrorKind.INVALID_PASSPHRASE,
    "400005": ErrorKind.INVALID_SIGNATURE,
    "400006": ErrorKind.IP_NOT_ALLOWED,
    "400007": ErrorKind.PERMISSION_DENIED,
    "429000": ErrorKind.RATE_LIMITED,
}
NOT_FOUND = ("order not exist", "order_not_exist", "the order does not exist")


def _map_permission(name: str) -> Permission:
    n = name.strip().lower()
    if n == "general":
        return Permission.READ
    if "withdraw" in n:
        return Permission.WITHDRAW
    if "transfer" in n:
        return Permission.TRANSFER
    if n in {"spot", "margin", "futures", "leveragedtoken", "earn"}:
        return Permission.TRADE
    return Permission.UNKNOWN


class KucoinConnector(SignedHttpConnector):
    exchange_id = "kucoin"
    base_url = "https://api.kucoin.com"
    rate_limits = {"account": (6, 1.0), "public": (10, 1.0), "trade": (6, 1.0)}

    @classmethod
    def guide(cls) -> WizardGuide:
        return WizardGuide(
            exchange_id="kucoin",
            display_name="KuCoin",
            api_key_page_url="https://www.kucoin.com/account/api",
            credential_fields=[
                CredentialField("api_key", "API key", "Shown after you create the key."),
                CredentialField("api_secret", "API secret", "Shown only once when you create the key. Copy all of it."),
                CredentialField("passphrase", "API passphrase", "The passphrase you typed when creating the key (not your login password)."),
            ],
            create_key_steps=[
                "Log in to KuCoin on the website and open Account, then API Management.",
                "Click Create API. Choose the API trading type (not Broker or Link).",
                "Give the key a name and set an API passphrase. Write the passphrase down; KuCoin will not show it again.",
                "Under permissions, keep only General (read). Leave Spot, Margin, Futures, Transfer and Withdrawal unticked for now.",
                "Turn on IP restriction and add our server IP address (shown below).",
                "Finish the security verification, then copy the API key and secret.",
            ],
            permissions_to_enable=["General"],
            permissions_to_never_enable=["Withdrawal", "Transfer"],
            ip_restriction_note="Restricting the key to our server's IP address means a leaked key is useless anywhere else.",
            notes=[
                "Spot trading permission is needed only for Real money on the Trade tab. You can add it later with Replace key.",
                "Paper trading on KuCoin uses this app's own simulator with real KuCoin prices.",
            ],
        )

    # --- signing -------------------------------------------------------------
    def _hmac_b64(self, msg: str) -> str:
        return base64.b64encode(
            hmac.new(self.credentials.api_secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()

    def _auth_headers(self, method: str, path: str, body: str) -> dict[str, str]:
        ts = str(self._now_ms())
        return {
            "KC-API-KEY": self.credentials.api_key,
            "KC-API-SIGN": self._hmac_b64(f"{ts}{method.upper()}{path}{body}"),
            "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": self._hmac_b64(self.credentials.passphrase),
            "KC-API-KEY-VERSION": "2",
        }

    def _unwrap(self, status: int, payload: Any) -> Any:
        if not isinstance(payload, dict):
            kind = ErrorKind.EXCHANGE_UNAVAILABLE if status >= 500 else ErrorKind.UNEXPECTED
            raise ExchangeError(kind, detail=f"http {status}")
        code = str(payload.get("code", ""))
        if code == "200000":
            return payload.get("data")
        if status == 429:
            raise ExchangeError(ErrorKind.RATE_LIMITED, code)
        kind = KUCOIN_ERRORS.get(code)
        if kind is None:
            kind = ErrorKind.EXCHANGE_UNAVAILABLE if status >= 500 else ErrorKind.UNEXPECTED
        raise ExchangeError(kind, code, str(payload.get("msg", ""))[:200])

    async def _server_time_ms(self) -> int:
        return int(await self._request("GET", "/api/v1/timestamp", group="public", signed=False))

    # --- read-only API -------------------------------------------------------
    async def get_key_info(self) -> KeyInfo:
        # First prove the key works with a plain read.
        await self._request("GET", "/api/v1/accounts", group="account", signed=True)
        try:
            info = await self._request("GET", "/api/v1/user/api-key", group="account", signed=True)
        except ExchangeError as exc:
            if exc.is_auth_failure and exc.kind != ErrorKind.PERMISSION_DENIED:
                raise
            # Permission list not readable for this account type. Report UNKNOWN so
            # the portal asks the user to confirm withdrawals are off.
            return KeyInfo(permissions={Permission.UNKNOWN}, raw_permissions=[], ip_restricted=None)
        raw = [p.strip() for p in str(info.get("permission", "")).split(",") if p.strip()]
        ip = info.get("ipWhitelist")
        return KeyInfo(
            permissions={_map_permission(p) for p in raw},
            raw_permissions=raw,
            ip_restricted=None if ip is None else bool(str(ip).strip()),
            account_id=str(info["uid"]) if info.get("uid") is not None else None,
        )

    async def get_balances(self) -> list[Balance]:
        accounts = await self._request("GET", "/api/v1/accounts", group="account", signed=True)
        totals: dict[str, list[Decimal]] = {}
        for a in accounts or []:
            t = totals.setdefault(a["currency"], [Decimal(0), Decimal(0)])
            t[0] += Decimal(a.get("balance") or "0")
            t[1] += Decimal(a.get("available") or "0")
        return [Balance(currency=c, total=str(t), available=str(av)) for c, (t, av) in sorted(totals.items())]

    async def get_trading_pairs(self) -> list[TradingPair]:
        data = await self._request("GET", "/api/v2/symbols", group="public", signed=False)
        return [
            TradingPair(
                symbol=s["symbol"],
                base=s["baseCurrency"],
                quote=s["quoteCurrency"],
                min_size=s.get("baseMinSize"),
                tick_size=s.get("priceIncrement"),
                lot_size=s.get("baseIncrement"),
            )
            for s in data or []
            if s.get("enableTrading")
        ]

    KUCOIN_TYPES = {"5m": "5min", "15m": "15min", "1h": "1hour", "4h": "4hour"}

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported timeframe {timeframe}")
        step = TIMEFRAME_SECONDS[timeframe]
        end = int(time.time())
        start = end - step * (limit + 1)
        path = f"/api/v1/market/candles?type={self.KUCOIN_TYPES[timeframe]}&symbol={symbol}&startAt={start}&endAt={end}"
        data = await self._request("GET", path, group="public", signed=False)
        # Rows: [time(s), open, close, high, low, volume, turnover], newest first.
        candles = sorted(
            (Candle(int(r[0]) * 1000, Decimal(r[1]), Decimal(r[3]), Decimal(r[4]), Decimal(r[2]), Decimal(r[5])) for r in data or []),
            key=lambda c: c.open_time_ms,
        )
        # KuCoin includes the candle still forming; keep closed candles only.
        return [c for c in candles if c.open_time_ms // 1000 + step <= end][-limit:]

    # --- spot orders (Real mode) -------------------------------------------------------
    # Accounts upgraded to KuCoin's "high-frequency" trading account must use the /hf/ order endpoints.
    _hf: bool | None = None

    async def _is_hf(self) -> bool:
        if self._hf is None:
            self._hf = bool(await self._request("GET", "/api/v1/hf/accounts/opened", group="account", signed=True))
        return self._hf

    async def place_market_order(self, symbol: str, side: str, qty: Decimal, client_id: str) -> str:
        body = {"clientOid": client_id, "side": side, "symbol": symbol, "type": "market", "size": f"{qty:f}"}
        path = "/api/v1/hf/orders" if await self._is_hf() else "/api/v1/orders"
        data = await self._request("POST", path, group="trade", signed=True, body=body, retry=False)
        return str(data["orderId"])

    async def get_order(self, symbol: str, order_id: str | None = None, client_id: str | None = None) -> OrderResult | None:
        if await self._is_hf():
            path = f"/api/v1/hf/orders/{order_id}?symbol={symbol}" if order_id else \
                f"/api/v1/hf/orders/client-order/{client_id}?symbol={symbol}"
        else:
            path = f"/api/v1/orders/{order_id}" if order_id else f"/api/v1/order/client-order/{client_id}"
        try:
            o = await self._request("GET", path, group="trade", signed=True)
        except ExchangeError as exc:
            if any(n in exc.detail.lower() for n in NOT_FOUND) or exc.exchange_code == "order_not_exist":
                return None
            raise
        if not o:
            return None
        filled = Decimal(str(o.get("dealSize") or "0"))
        funds = Decimal(str(o.get("dealFunds") or "0"))
        avg = funds / filled if filled else None
        fee = Decimal(str(o.get("fee") or "0"))
        base, _, quote = symbol.partition("-")
        if o.get("feeCurrency") == base:
            if o.get("side") == "buy":
                filled -= fee
            fee_quote = fee * (avg or 0)
        else:
            fee_quote = fee if o.get("feeCurrency") == quote else Decimal(0)
        active = o.get("isActive", o.get("active"))
        status = "open" if active else "canceled" if o.get("cancelExist") else "filled"
        return OrderResult(str(o.get("id") or order_id), o.get("clientOid") or client_id, status, filled, avg, fee_quote)
