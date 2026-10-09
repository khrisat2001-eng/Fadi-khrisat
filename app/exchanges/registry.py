"""Registered exchanges. Add new connectors here."""
from __future__ import annotations

from .base import ExchangeConnector
from .kucoin import KucoinConnector
from .okx import OkxConnector

CONNECTORS: dict[str, type[ExchangeConnector]] = {
    OkxConnector.exchange_id: OkxConnector,
    KucoinConnector.exchange_id: KucoinConnector,
}


def get_connector_class(exchange_id: str) -> type[ExchangeConnector]:
    try:
        return CONNECTORS[exchange_id]
    except KeyError:
        raise ValueError(f"Unsupported exchange: {exchange_id}") from None
