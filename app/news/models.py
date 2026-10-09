"""Shared vocabulary for the news engine. Every enum here is closed: anything else is rejected."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

CATEGORIES = {
    "A": "Crypto-specific",
    "B": "Regulatory and legal",
    "C": "Macro and financial",
    "D": "Market-specific",
}

# Sub-categories carry the safety meaning. Each belongs to one top-level category.
SUBCATEGORIES = {
    "protocol_upgrade": "A", "network_outage": "A", "listing": "A", "delisting": "A",
    "exchange_maintenance": "A", "exchange_incident": "A", "security_incident": "A", "token_unlock": "A",
    "partnership": "A", "funding_acquisition": "A", "supply_change": "A", "project_news": "A",
    "regulation": "B", "enforcement": "B", "etf": "B", "court_decision": "B", "trading_restriction": "B",
    "central_bank": "C", "inflation": "C", "employment_growth": "C", "macro_other": "C", "geopolitical": "C",
    "volume_anomaly": "D", "liquidity_change": "D", "volatility": "D", "price_move": "D",
    "other": "A",
}

SENTIMENTS = ("strongly_positive", "moderately_positive", "neutral", "moderately_negative", "strongly_negative", "uncertain")
SENTIMENT_SCORE = {"strongly_positive": 1.0, "moderately_positive": 0.5, "neutral": 0.0,
                   "moderately_negative": -0.5, "strongly_negative": -1.0, "uncertain": 0.0}
SEVERITIES = ("low", "medium", "high", "critical")
HORIZONS = ("minutes", "hours", "days", "weeks")

# Deterministic confirmation status (never decided by the model).
CONFIRMED = "confirmed"
CREDIBLE = "credible_unconfirmed"
RUMOR = "rumor"
DISPUTED = "disputed"
CONFIRMATION_LABELS = {CONFIRMED: "Confirmed", CREDIBLE: "Credible, unconfirmed", RUMOR: "Rumor / speculative", DISPUTED: "Disputed / likely false"}
CONFIRMATION_FACTOR = {CONFIRMED: 1.0, CREDIBLE: 0.5, RUMOR: 0.0, DISPUTED: 0.0}

TIER_LABELS = {1: "Official / primary", 2: "Reputable outlet", 3: "Aggregator", 4: "Social / unknown"}


@dataclass
class RawItem:
    """One item as fetched from a source, before any processing. All text is untrusted."""

    source_id: str
    url: str
    headline: str
    body: str
    published_at: datetime | None
    language: str = "en"
    external_id: str | None = None
    tags: list[str] = field(default_factory=list)
