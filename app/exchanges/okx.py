"""OKX (API v5) read-only connector.

Signing: base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + body)),
timestamp in ISO 8601 UTC with milliseconds.
Docs: https://www.okx.com/docs-v5/en/
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
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
    Permission,
    TradingPair,
    WizardGuide,
)
from .http import SignedHttpConnector

# OKX error codes we translate. Re-check against the docs' error-code table when
# OKX publishes changes; unknown codes fall back to UNEXPECTED.
OKX_ERRORS = {
    "50001": ErrorKind.EXCHANGE_UNAVAILABLE,
    "50013": ErrorKind.EXCHANGE_UNAVAILABLE,
    "50011": ErrorKind.RATE_LIMITED,
    "50061": ErrorKind.RATE_LIMITED,
    "50102": ErrorKind.CLOCK_SKEW,
    "50112": ErrorKind.CLOCK_SKEW,
    "50105": ErrorKind.INVALID_PASSPHRASE,
    "50110": ErrorKind.IP_NOT_ALLOWED,
    "50111": ErrorKind.INVALID_KEY,
    "50113": ErrorKind.INVALID_SIGNATURE,
    "50114": ErrorKind.INVALID_KEY,
}

OKX_PERMS = {
    "read_only": Permission.READ,
    "trade": Permission.TRADE,
    "withdraw": Permission.WITHDRAW,
}


class OkxConnector(SignedHttpConnector):
    exchange_id = "okx"
    base_url = "https://www.okx.com"
    rate_limits = {"account": (4, 2.0), "public": (10, 2.0)}

    @classmethod
    def guide(cls) -> WizardGuide:
        return WizardGuide(
            exchange_id="okx",
            display_name="OKX",
            api_key_page_url="https://www.okx.com/account/my-api",
            credential_fields=[
                CredentialField("api_key", "API key", "Shown after you create the key."),
                CredentialField("api_secret", "Secret key", "Shown only once when you create the key. Copy all of it."),
                CredentialField("passphrase", "Passphrase", "The passphrase you typed when creating the key. OKX cannot show it again."),
            ],
            create_key_steps=[
                "Log in to OKX on the website and open Profile, then API (the API management page).",
                "Click Create API key. Choose the main account or a sub-account you use for the bot.",
                "Give the key a name you'll recognise, for example \"Trading app (read only)\".",
                "Set a passphrase and write it down somewhere safe. OKX will not show it again.",
                "Under permissions, tick Read only. Leave Trade and Withdraw unticked for now.",
                "Add our server IP address under IP addresses (shown below).",
                "Confirm with your 2FA code, then copy the API key and secret key.",
            ],
            permissions_to_enable=["Read"],
            permissions_to_never_enable=["Withdraw"],
            ip_restriction_note=(
                "Binding the key to our server's IP address means a leaked key is useless anywhere else. "
                "OKX's documentation also says keys with Trade or Withdraw permission and no bound IP can be "
                "deleted automatically after a period of inactivity."
            ),
            notes=[
                "Trade permission is only needed later, if you decide to enable live trading after paper trading passes.",
                "OKX is not available in every country. Check OKX's own eligibility rules for where you live.",
            ],
        )

    # --- signing -------------------------------------------------------------
    def _timestamp(self) -> str:
        dt = datetime.fromtimestamp(self._now_ms() / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def _auth_headers(self, method: str, path: str, body: str) -> dict[str, str]:
        ts = self._timestamp()
        prehash = f"{ts}{method.upper()}{path}{body}"
        sign = base64.b64encode(
            hmac.new(self.credentials.api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "OK-ACCESS-KEY": self.credentials.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.credentials.passphrase,
        }

    def _unwrap(self, status: int, payload: Any) -> Any:
        if not isinstance(payload, dict):
            kind = ErrorKind.EXCHANGE_UNAVAILABLE if status >= 500 else ErrorKind.UNEXPECTED
            raise ExchangeError(kind, detail=f"http {status}")
        code = str(payload.get("code", ""))
        if code == "0":
            return payload.get("data", [])
        if status == 429:
            raise ExchangeError(ErrorKind.RATE_LIMITED, code)
        kind = OKX_ERRORS.get(code)
        if kind is None:
            kind = ErrorKind.EXCHANGE_UNAVAILABLE if status >= 500 else ErrorKind.UNEXPECTED
        raise ExchangeError(kind, code, str(payload.get("msg", ""))[:200])

    async def _server_time_ms(self) -> int:
        data = await self._request("GET", "/api/v5/public/time", group="public", signed=False)
        return int(data[0]["ts"])

    # --- read-only API -------------------------------------------------------
    async def get_key_info(self) -> KeyInfo:
        data = await self._request("GET", "/api/v5/account/config", group="account", signed=True)
        cfg = data[0] if data else {}
        raw = [p.strip() for p in str(cfg.get("perm", "")).split(",") if p.strip()]
        perms = {OKX_PERMS.get(p, Permission.UNKNOWN) for p in raw}
        ip = cfg.get("ip")
        return KeyInfo(
            permissions=perms,
            raw_permissions=raw,
            ip_restricted=None if ip is None else bool(str(ip).strip()),
            account_id=cfg.get("uid"),
        )

    async def get_balances(self) -> list[Balance]:
        data = await self._request("GET", "/api/v5/account/balance", group="account", signed=True)
        details = data[0].get("details", []) if data else []
        return [
            Balance(currency=d["ccy"], total=d.get("cashBal") or d.get("eq") or "0", available=d.get("availBal") or "0")
            for d in details
        ]

    async def get_trading_pairs(self) -> list[TradingPair]:
        data = await self._request("GET", "/api/v5/public/instruments?instType=SPOT", group="public", signed=False)
        return [
            TradingPair(symbol=i["instId"], base=i["baseCcy"], quote=i["quoteCcy"], min_size=i.get("minSz"), tick_size=i.get("tickSz"))
            for i in data
            if i.get("state") == "live"
        ]

    OKX_BARS = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H"}

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported timeframe {timeframe}")
        path = f"/api/v5/market/candles?instId={symbol}&bar={self.OKX_BARS[timeframe]}&limit={min(limit, 300)}"
        data = await self._request("GET", path, group="public", signed=False)
        # Rows: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm], newest first. confirm "1" = closed.
        rows = [r for r in data if len(r) < 9 or r[8] == "1"]
        return sorted(
            (Candle(int(r[0]), Decimal(r[1]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4]), Decimal(r[5])) for r in rows),
            key=lambda c: c.open_time_ms,
        )
