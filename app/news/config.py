"""User-editable news settings. News can only make trading more careful, so every
size multiplier here is capped at 1."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class NoTradeWindow(BaseModel):
    before_minutes: int = Field(ge=0, le=1440)
    after_minutes: int = Field(ge=0, le=1440)


class NewsConfig(BaseModel):
    poll_interval_seconds: int = Field(300, ge=60, le=86400)
    # Items about the same thing within this many hours join one event.
    cluster_window_hours: int = Field(48, ge=1, le=336)
    cluster_similarity: float = Field(0.5, ge=0.2, le=0.95)
    # A new item matching an older event (beyond the cluster window) is a repeat, not a new catalyst.
    repeat_lookback_days: int = Field(30, ge=1, le=365)
    # Ignore news older than this for trading purposes (it is still shown on the dashboard).
    max_news_age_hours: int = Field(72, ge=1, le=720)
    # Adaptive confirmation window (unverified news hold, positive news waiting for price confirmation):
    # strategy candles x severity x source tier x liquidity x volatility, bounded by these limits.
    confirmation_candles: int = Field(3, ge=1, le=20)
    min_confirmation_minutes: int = Field(15, ge=1, le=1440)
    max_confirmation_minutes: int = Field(720, ge=5, le=4320)
    # Confirmed negative high-severity news blocks new buys in the asset for this long.
    negative_block_hours: int = Field(24, ge=1, le=336)
    # Position size multipliers for uncertain news and for news that raises volatility (never above 1).
    uncertain_size_multiplier: float = Field(0.5, ge=0.0, le=1.0)
    volatile_size_multiplier: float = Field(0.5, ge=0.0, le=1.0)
    # When no source has fetched successfully for this long, the news check reports it.
    feeds_stale_minutes: int = Field(60, ge=5, le=1440)
    block_when_feeds_stale: bool = False
    # Credible security incidents and exchange disruptions suspend entries automatically.
    auto_suspend_on_incidents: bool = True
    no_trade_windows: dict[str, NoTradeWindow] = Field(default_factory=lambda: {
        "high": NoTradeWindow(before_minutes=30, after_minutes=30),
        "medium": NoTradeWindow(before_minutes=15, after_minutes=15),
        "low": NoTradeWindow(before_minutes=0, after_minutes=0),
    })
    # Calendar entries not re-verified within this many days are shown as uncertain.
    calendar_stale_days: int = Field(7, ge=1, le=90)

    @model_validator(mode="after")
    def _check(self):
        if self.min_confirmation_minutes > self.max_confirmation_minutes:
            raise ValueError("min_confirmation_minutes must not exceed max_confirmation_minutes")
        if set(self.no_trade_windows) - {"low", "medium", "high"}:
            raise ValueError("no_trade_windows keys must be low, medium or high")
        return self
