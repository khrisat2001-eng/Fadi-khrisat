"""Asset and exchange attribution.

Only assets in the registry can be attributed. A full name or alias match is
strong evidence. A bare ticker is accepted only when it is not an ordinary
word; ambiguous tickers (LINK, DOT, NEAR, ...) need a cashtag ($LINK), a pair
(LINK-USDT) or the project's name. The model may suggest assets, but code
decides, and suggestions outside the registry are dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Asset:
    code: str
    names: tuple[str, ...]
    ambiguous_ticker: bool = False


DEFAULT_ASSETS: tuple[Asset, ...] = (
    Asset("BTC", ("bitcoin",)),
    Asset("ETH", ("ethereum", "ether")),
    Asset("SOL", ("solana",)),
    Asset("XRP", ("xrp ledger",)),
    Asset("BNB", ("bnb chain",)),
    Asset("DOGE", ("dogecoin",)),
    Asset("ADA", ("cardano",)),
    Asset("TRX", ("tron",)),
    Asset("AVAX", ("avalanche",)),
    Asset("LINK", ("chainlink",), ambiguous_ticker=True),
    Asset("DOT", ("polkadot",), ambiguous_ticker=True),
    Asset("TON", ("toncoin",), ambiguous_ticker=True),
    Asset("LTC", ("litecoin",)),
    Asset("NEAR", ("near protocol",), ambiguous_ticker=True),
    Asset("UNI", ("uniswap",), ambiguous_ticker=True),
    Asset("OP", ("optimism",), ambiguous_ticker=True),
    Asset("ARB", ("arbitrum",)),
    Asset("SUI", ("sui network",), ambiguous_ticker=True),
    Asset("APT", ("aptos",), ambiguous_ticker=True),
    Asset("OKB", ()),
    Asset("KCS", ("kucoin token",)),
    Asset("USDT", ("tether",)),
    Asset("USDC", ("usd coin",)),
)

EXCHANGES = {"okx": ("okx", "okex"), "kucoin": ("kucoin",)}


@dataclass
class Attribution:
    assets: dict[str, str] = field(default_factory=dict)  # code -> "name" | "ticker"
    exchanges: list[str] = field(default_factory=list)


class AssetRegistry:
    def __init__(self, assets: tuple[Asset, ...] = DEFAULT_ASSETS):
        self._assets = {a.code: a for a in assets}

    def add_ticker(self, code: str) -> None:
        """Pairs the user trades but the registry doesn't know: ticker-only and treated as ambiguous."""
        code = code.upper()
        if code not in self._assets and re.fullmatch(r"[A-Z0-9]{2,12}", code):
            self._assets[code] = Asset(code, (), ambiguous_ticker=True)

    def known(self, code: str) -> bool:
        return code.upper() in self._assets

    def codes(self) -> list[str]:
        return sorted(self._assets)

    def attribute(self, text: str) -> Attribution:
        out = Attribution()
        lower = text.lower()
        for a in self._assets.values():
            if any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", lower) for n in a.names):
                out.assets[a.code] = "name"
                continue
            code = re.escape(a.code)
            strong = re.search(rf"\${code}\b|\b{code}[-/](USDT|USDC|USD|BTC|ETH)\b", text)
            plain = re.search(rf"(?<![\w$-]){code}(?![\w-])", text)  # exact uppercase only
            if strong or (plain and not a.ambiguous_ticker):
                out.assets[a.code] = "ticker"
        for ex, names in EXCHANGES.items():
            if any(re.search(rf"\b{re.escape(n)}\b", lower) for n in names):
                out.exchanges.append(ex)
        return out
