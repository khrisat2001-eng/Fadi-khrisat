"""Connection lifecycle: validate, store, test, sync, replace, disconnect.

States
  REJECTED            key failed validation or has forbidden permissions (credentials deleted)
  CONNECTED_READONLY  key validated; balances visible; nothing trades
  PAPER               market data + paper trading enabled by the user
  DISCONNECTED        user disconnected (credentials deleted)

Health (independent of state): ok, degraded (exchange unreachable), auth_failed.

Live trading does not exist in this module. request_live() always refuses.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from app.exchanges.base import (
    Credentials,
    ExchangeConnector,
    ExchangeError,
    KeyInfo,
    Permission,
)
from app.exchanges.registry import CONNECTORS, get_connector_class
from app.security.vault import CredentialVault

from .store import Store, now_iso

ConnectorFactory = Callable[[str, Credentials | None], ExchangeConnector]

ACTIVE_STATES = {"CONNECTED_READONLY", "PAPER"}


def default_connector_factory(exchange_id: str, creds: Credentials | None) -> ExchangeConnector:
    return get_connector_class(exchange_id)(creds)


class PortalError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)


@dataclass
class Issue:
    level: str  # "error" | "warning" | "info"
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


def audit_permissions(exchange_id: str, info: KeyInfo) -> list[Issue]:
    guide = CONNECTORS[exchange_id].guide()
    issues: list[Issue] = []
    if Permission.WITHDRAW in info.permissions:
        issues.append(Issue("error", "withdraw_enabled",
            f"This key can withdraw funds. For your safety we don't accept keys with withdrawal permission. "
            f"Please delete it on {guide.display_name} and create a new key without {', '.join(guide.permissions_to_never_enable)}."))
    if Permission.TRANSFER in info.permissions:
        issues.append(Issue("error", "transfer_enabled",
            "This key can transfer funds between accounts. This app never moves funds, so please create a key without Transfer permission."))
    if Permission.TRADE in info.permissions:
        issues.append(Issue("warning", "trade_enabled",
            "This key can place trades. That isn't needed yet: market data and paper trading only need read access. "
            "We recommend replacing it with a read-only key until you decide on live trading."))
    if Permission.UNKNOWN in info.permissions:
        issues.append(Issue("warning", "permissions_unverified",
            f"We couldn't read this key's full permission list from {guide.display_name}. "
            "Please double-check on the exchange that Withdrawal and Transfer are turned off."))
    if info.ip_restricted is False:
        issues.append(Issue("warning", "no_ip_restriction",
            "This key isn't restricted to our server's IP address. Adding the IP restriction makes a leaked key useless elsewhere."))
    elif info.ip_restricted is None:
        issues.append(Issue("info", "ip_restriction_unknown",
            "We couldn't check whether this key has an IP restriction. We recommend adding one."))
    return issues


def _blocking(issues: list[Issue]) -> bool:
    return any(i.level == "error" for i in issues)


class ConnectionService:
    def __init__(self, store: Store, vault: CredentialVault, connector_factory: ConnectorFactory = default_connector_factory):
        self.store = store
        self.vault = vault
        self.factory = connector_factory

    # --- helpers -------------------------------------------------------------
    def _get(self, connection_id: str) -> dict[str, Any]:
        row = self.store.get_connection(connection_id)
        if row is None:
            raise PortalError("Connection not found.", 404)
        return row

    def _credentials(self, row: dict[str, Any]) -> Credentials:
        enc = self.store.get_credentials(row["id"], row["key_hint"])
        if enc is None:
            raise PortalError("This connection has no stored key. Please connect again.", 409)
        return self.vault.decrypt(row["id"], row["exchange"], enc)

    async def _probe(self, exchange_id: str, creds: Credentials, *, with_pairs: bool):
        connector = self.factory(exchange_id, creds)
        try:
            info = await connector.get_key_info()
            balances = await connector.get_balances()
            pairs = await connector.get_trading_pairs() if with_pairs else None
            return info, balances, pairs
        finally:
            await connector.aclose()

    @staticmethod
    def _validate_input(creds: Credentials) -> None:
        for name, value in (("API key", creds.api_key), ("Secret", creds.api_secret), ("Passphrase", creds.passphrase)):
            if not value or not value.strip():
                raise PortalError(f"{name} is required.")
            if len(value) > 256 or any(c.isspace() for c in value.strip()):
                raise PortalError(f"{name} doesn't look right. Copy it again without extra spaces.")

    # --- wizard --------------------------------------------------------------
    async def connect(self, exchange_id: str, creds: Credentials) -> dict[str, Any]:
        if exchange_id not in CONNECTORS:
            raise PortalError("Unsupported exchange.")
        creds = Credentials(creds.api_key.strip(), creds.api_secret.strip(), creds.passphrase.strip())
        self._validate_input(creds)
        for existing in self.store.list_connections():
            if existing["exchange"] == exchange_id and existing["state"] in ACTIVE_STATES:
                raise PortalError("This exchange is already connected. Use Replace key or Disconnect first.", 409)

        connection_id = str(uuid.uuid4())
        enc = self.vault.encrypt(connection_id, exchange_id, creds)
        self.store.insert_connection(
            {"id": connection_id, "exchange": exchange_id, "state": "PENDING_VALIDATION", "key_hint": enc.key_hint}
        )
        self.store.put_credentials(connection_id, enc)
        self.store.audit(connection_id, "key_submitted", f"key …{enc.key_hint}")

        try:
            info, balances, pairs = await self._probe(exchange_id, creds, with_pairs=True)
        except ExchangeError as exc:
            self._reject(connection_id, [Issue("error", exc.kind.value, exc.plain_message)], last_error=exc.plain_message)
            return self.view(connection_id)

        issues = audit_permissions(exchange_id, info)
        if _blocking(issues):
            self._reject(connection_id, issues, permissions=info)
            return self.view(connection_id)

        self.store.update_connection(
            connection_id,
            state="CONNECTED_READONLY",
            health="ok",
            permissions=sorted(p.value for p in info.permissions),
            raw_permissions=info.raw_permissions,
            ip_restricted=info.ip_restricted,
            account_id=info.account_id,
            issues=[i.as_dict() for i in issues],
            balances=[b.__dict__ for b in balances],
            available_pairs=[p.__dict__ for p in pairs],
            last_sync_at=now_iso(),
            last_test_at=now_iso(),
            last_error=None,
        )
        self.store.audit(connection_id, "validated", f"permissions={info.raw_permissions}")
        return self.view(connection_id)

    def _reject(self, connection_id: str, issues: list[Issue], *, permissions: KeyInfo | None = None, last_error: str | None = None) -> None:
        self.store.delete_credentials(connection_id)
        fields: dict[str, Any] = {"state": "REJECTED", "issues": [i.as_dict() for i in issues], "last_error": last_error}
        if permissions is not None:
            fields["permissions"] = sorted(p.value for p in permissions.permissions)
            fields["raw_permissions"] = permissions.raw_permissions
        self.store.update_connection(connection_id, **fields)
        self.store.audit(connection_id, "rejected", "; ".join(i.code for i in issues))

    def update_settings(self, connection_id: str, allocation_pct: float, pairs: list[str]) -> dict[str, Any]:
        row = self._get(connection_id)
        if row["state"] not in ACTIVE_STATES:
            raise PortalError("Settings can only be changed on an active connection.", 409)
        if not 0 < allocation_pct <= 100:
            raise PortalError("Allocation must be between 0 and 100 percent.")
        available = {p["symbol"] for p in row["available_pairs"]}
        unknown = [p for p in pairs if p not in available]
        if unknown:
            raise PortalError(f"These pairs aren't available on this exchange: {', '.join(unknown[:5])}")
        if not pairs:
            raise PortalError("Pick at least one trading pair.")
        self.store.update_connection(connection_id, allocation_pct=allocation_pct, selected_pairs=sorted(set(pairs)))
        self.store.audit(connection_id, "settings_changed", f"allocation={allocation_pct}% pairs={len(pairs)}")
        return self.view(connection_id)

    def enable_paper(self, connection_id: str) -> dict[str, Any]:
        row = self._get(connection_id)
        if row["state"] != "CONNECTED_READONLY":
            raise PortalError("Paper trading can be enabled only on a validated connection.", 409)
        if row["health"] != "ok":
            raise PortalError("Fix the connection problem first, then try again.", 409)
        if not row["selected_pairs"] or not row["allocation_pct"]:
            raise PortalError("Choose an allocation and at least one trading pair first.")
        self.store.update_connection(connection_id, state="PAPER", trading_mode="paper")
        self.store.audit(connection_id, "paper_enabled")
        return self.view(connection_id)

    def request_live(self, connection_id: str) -> None:
        self._get(connection_id)
        self.store.audit(connection_id, "live_request_refused")
        raise PortalError(
            "Live trading is locked. It can only be unlocked after the strategy, news monitoring, risk controls "
            "and paper trading pass their acceptance tests, and only with your explicit authorization.",
            403,
        )

    # --- management ----------------------------------------------------------
    async def test(self, connection_id: str) -> dict[str, Any]:
        row = self._get(connection_id)
        if row["state"] not in ACTIVE_STATES:
            raise PortalError("Only active connections can be tested.", 409)
        creds = self._credentials(row)
        try:
            info, balances, _ = await self._probe(row["exchange"], creds, with_pairs=False)
        except ExchangeError as exc:
            self._record_failure(row, exc)
            return self.view(connection_id)

        issues = audit_permissions(row["exchange"], info)
        if _blocking(issues):
            # Permissions were changed on the exchange after connecting.
            self._reject(connection_id, issues, permissions=info)
            self.store.add_alert(connection_id, "critical",
                f"{row['exchange'].upper()} key now has a forbidden permission. The connection was stopped and the key deleted from our server.")
            return self.view(connection_id)

        if row["health"] != "ok":
            self.store.add_alert(connection_id, "info", f"{row['exchange'].upper()} connection is working again.")
        self.store.update_connection(
            connection_id,
            health="ok",
            permissions=sorted(p.value for p in info.permissions),
            raw_permissions=info.raw_permissions,
            ip_restricted=info.ip_restricted,
            issues=[i.as_dict() for i in issues],
            balances=[b.__dict__ for b in balances],
            last_sync_at=now_iso(),
            last_test_at=now_iso(),
            last_error=None,
        )
        return self.view(connection_id)

    def _record_failure(self, row: dict[str, Any], exc: ExchangeError) -> None:
        health = "auth_failed" if exc.is_auth_failure else "degraded"
        if row["health"] != health:
            level = "critical" if health == "auth_failed" else "warning"
            prefix = "Authentication failed" if health == "auth_failed" else "Connection interrupted"
            self.store.add_alert(row["id"], level, f"{row['exchange'].upper()}: {prefix}. {exc.plain_message}")
        self.store.update_connection(row["id"], health=health, last_error=exc.plain_message, last_test_at=now_iso())
        self.store.audit(row["id"], "test_failed", exc.kind.value)

    async def sync_balances(self, connection_id: str) -> dict[str, Any]:
        # Balances come from the same read calls as a test.
        return await self.test(connection_id)

    async def replace_credentials(self, connection_id: str, creds: Credentials) -> dict[str, Any]:
        row = self._get(connection_id)
        if row["state"] not in ACTIVE_STATES:
            raise PortalError("Only active connections can have their key replaced. Connect again instead.", 409)
        creds = Credentials(creds.api_key.strip(), creds.api_secret.strip(), creds.passphrase.strip())
        self._validate_input(creds)
        try:
            info, balances, _ = await self._probe(row["exchange"], creds, with_pairs=False)
        except ExchangeError as exc:
            raise PortalError(f"The new key didn't work, so we kept the old one. {exc.plain_message}") from None
        issues = audit_permissions(row["exchange"], info)
        if _blocking(issues):
            raise PortalError("The new key was not accepted, so we kept the old one. " + " ".join(i.message for i in issues if i.level == "error"))

        enc = self.vault.encrypt(connection_id, row["exchange"], creds)
        self.store.put_credentials(connection_id, enc)  # old ciphertext and data key are overwritten
        self.store.update_connection(
            connection_id,
            key_hint=enc.key_hint,
            health="ok",
            permissions=sorted(p.value for p in info.permissions),
            raw_permissions=info.raw_permissions,
            ip_restricted=info.ip_restricted,
            issues=[i.as_dict() for i in issues],
            balances=[b.__dict__ for b in balances],
            last_sync_at=now_iso(),
            last_test_at=now_iso(),
            last_error=None,
        )
        self.store.audit(connection_id, "key_replaced", f"old …{row['key_hint']} new …{enc.key_hint}")
        return self.view(connection_id)

    def disconnect(self, connection_id: str) -> dict[str, Any]:
        row = self._get(connection_id)
        self.store.delete_credentials(connection_id)
        self.store.update_connection(connection_id, state="DISCONNECTED", health="unknown", balances=[])
        self.store.audit(connection_id, "disconnected", f"key …{row['key_hint']} deleted")
        return self.view(connection_id)

    async def check_all(self) -> None:
        """Called periodically by the health monitor."""
        for row in self.store.list_connections():
            if row["state"] in ACTIVE_STATES:
                try:
                    await self.test(row["id"])
                except PortalError:
                    pass

    # --- read model ----------------------------------------------------------
    def view(self, connection_id: str) -> dict[str, Any]:
        row = self._get(connection_id)
        return {
            "id": row["id"],
            "exchange": row["exchange"],
            "state": row["state"],
            "health": row["health"],
            "trading_mode": row["trading_mode"],
            "live_trading": False,
            "key_masked": f"••••••••{row['key_hint']}" if row["key_hint"] else None,
            "permissions": row["permissions"],
            "raw_permissions": row["raw_permissions"],
            "ip_restricted": row["ip_restricted"],
            "issues": row["issues"],
            "balances": row["balances"],
            "available_pairs": [p["symbol"] for p in row["available_pairs"]],
            "selected_pairs": row["selected_pairs"],
            "allocation_pct": row["allocation_pct"],
            "last_error": row["last_error"],
            "last_sync_at": row["last_sync_at"],
            "last_test_at": row["last_test_at"],
            "created_at": row["created_at"],
        }

    def list_views(self) -> list[dict[str, Any]]:
        return [self.view(r["id"]) for r in self.store.list_connections()]
