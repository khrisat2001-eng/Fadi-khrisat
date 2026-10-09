"""Exchange connector interface shared by every exchange adapter.

Adding a new exchange means writing one subclass of ExchangeConnector and
registering it in registry.py. Nothing else in the platform needs to change.

Connectors in this module are read-only. There are deliberately no methods for
placing orders, transferring funds or withdrawing.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class Permission(str, enum.Enum):
    READ = "read"
    TRADE = "trade"
    TRANSFER = "transfer"
    WITHDRAW = "withdraw"
    UNKNOWN = "unknown"


class ErrorKind(str, enum.Enum):
    INVALID_KEY = "invalid_key"
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_PASSPHRASE = "invalid_passphrase"
    IP_NOT_ALLOWED = "ip_not_allowed"
    PERMISSION_DENIED = "permission_denied"
    CLOCK_SKEW = "clock_skew"
    RATE_LIMITED = "rate_limited"
    EXCHANGE_UNAVAILABLE = "exchange_unavailable"
    NETWORK = "network"
    UNEXPECTED = "unexpected"


# Errors that mean the stored credentials no longer work.
AUTH_ERROR_KINDS = {
    ErrorKind.INVALID_KEY,
    ErrorKind.INVALID_SIGNATURE,
    ErrorKind.INVALID_PASSPHRASE,
    ErrorKind.IP_NOT_ALLOWED,
    ErrorKind.PERMISSION_DENIED,
}

PLAIN_MESSAGES = {
    ErrorKind.INVALID_KEY: "The exchange doesn't recognise this API key. It may have been deleted or expired. Please create a new key.",
    ErrorKind.INVALID_SIGNATURE: "The secret key doesn't match this API key. Please check you copied the full secret.",
    ErrorKind.INVALID_PASSPHRASE: "The passphrase is wrong. Use the passphrase you chose when you created this API key.",
    ErrorKind.IP_NOT_ALLOWED: "The exchange blocked this request because our server's IP address isn't on the key's allowed list. Add our server IP to the key's IP restriction.",
    ErrorKind.PERMISSION_DENIED: "This API key doesn't have permission for this action.",
    ErrorKind.CLOCK_SKEW: "Our server clock and the exchange clock disagree. We'll resync and try again.",
    ErrorKind.RATE_LIMITED: "The exchange is limiting how fast we can send requests. We'll slow down and retry.",
    ErrorKind.EXCHANGE_UNAVAILABLE: "The exchange isn't responding right now. This is usually temporary.",
    ErrorKind.NETWORK: "We couldn't reach the exchange. Check back in a few minutes.",
    ErrorKind.UNEXPECTED: "The exchange returned an unexpected error.",
}


class ExchangeError(Exception):
    def __init__(self, kind: ErrorKind, exchange_code: str | None = None, detail: str = ""):
        self.kind = kind
        self.exchange_code = exchange_code
        self.detail = detail
        super().__init__(f"{kind.value} (code={exchange_code}) {detail}".strip())

    @property
    def plain_message(self) -> str:
        return PLAIN_MESSAGES[self.kind]

    @property
    def is_auth_failure(self) -> bool:
        return self.kind in AUTH_ERROR_KINDS


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str
    passphrase: str

    def __repr__(self) -> str:  # never print secrets, even in tracebacks
        return f"Credentials(api_key=…{self.api_key[-4:]}, api_secret=***, passphrase=***)"

    __str__ = __repr__


@dataclass
class KeyInfo:
    permissions: set[Permission]
    raw_permissions: list[str]
    ip_restricted: bool | None  # None = exchange didn't tell us
    account_id: str | None = None


@dataclass
class Balance:
    currency: str
    total: str
    available: str


@dataclass
class TradingPair:
    symbol: str
    base: str
    quote: str
    min_size: str | None = None
    tick_size: str | None = None


@dataclass
class CredentialField:
    name: str
    label: str
    help: str


@dataclass
class WizardGuide:
    """Plain-language content the connection wizard shows for one exchange."""

    exchange_id: str
    display_name: str
    api_key_page_url: str
    credential_fields: list[CredentialField]
    create_key_steps: list[str]
    permissions_to_enable: list[str]
    permissions_to_never_enable: list[str]
    ip_restriction_note: str
    notes: list[str] = field(default_factory=list)


class ExchangeConnector(ABC):
    exchange_id: str

    @classmethod
    @abstractmethod
    def guide(cls) -> WizardGuide: ...

    @abstractmethod
    async def get_key_info(self) -> KeyInfo:
        """Read-only call returning the key's permissions and IP binding."""

    @abstractmethod
    async def get_balances(self) -> list[Balance]: ...

    @abstractmethod
    async def get_trading_pairs(self) -> list[TradingPair]:
        """Public spot instruments currently open for trading."""

    @abstractmethod
    async def aclose(self) -> None: ...
