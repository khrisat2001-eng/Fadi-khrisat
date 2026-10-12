from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class StrategyConfig(BaseModel):
    """Trend breakout strategy settings."""

    timeframe: str = Field("15m", pattern="^(5m|15m|1h|4h)$")
    breakout_lookback: int = Field(20, ge=5, le=200, description="Candles used for support/resistance")
    ema_fast: int = Field(20, ge=2, le=200)
    ema_slow: int = Field(50, ge=5, le=400)
    rsi_period: int = Field(14, ge=2, le=50)
    rsi_min: float = Field(50, ge=0, le=100, description="Momentum must be at least this")
    rsi_max: float = Field(75, ge=0, le=100, description="Above this the move is treated as overheated")
    volume_multiple: float = Field(1.5, ge=0.5, le=10, description="Breakout candle volume vs average")
    atr_period: int = Field(14, ge=2, le=50)
    stop_atr: float = Field(2.0, gt=0, le=10, description="Suggested stop = entry - this × ATR")
    target_atr: float = Field(6.0, gt=0, le=30, description="Suggested target = entry + this × ATR")
    min_stop_pct: float = Field(0.6, ge=0, le=10, description="Stop is never closer than this % of price, so fees "
                                "don't eat the edge; the target widens by the same ratio")
    max_breakout_extension_atr: float = Field(1.0, gt=0, le=10, description="Too far above the breakout level")
    max_ema_extension_atr: float = Field(2.5, gt=0, le=20, description="Too far above the fast EMA")
    exit_on_trend_break: bool = Field(True, description="Autopilot sells when a candle closes below the fast EMA")
    trailing_stop: bool = Field(True, description="Autopilot raises the stop as price rises (never lowers it)")
    max_candle_age_multiple: float = Field(2.0, ge=1, le=10, description="Candles older than this many bars are stale")

    @model_validator(mode="after")
    def _check(self):
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be shorter than ema_slow")
        if self.rsi_min >= self.rsi_max:
            raise ValueError("rsi_min must be below rsi_max")
        return self

    @property
    def candles_needed(self) -> int:
        return max(self.ema_slow, self.breakout_lookback, self.rsi_period, self.atr_period) + 30


# Trading styles shown as one switch on the Trade screen. Each is a set of strategy settings;
# anything else the user changed under Advanced is kept.
STYLES: dict[str, dict] = {
    "careful": {"breakout_lookback": 20, "volume_multiple": 1.5, "rsi_min": 50, "rsi_max": 75,
                "max_breakout_extension_atr": 1.0, "max_ema_extension_atr": 2.5},
    "active": {"breakout_lookback": 10, "volume_multiple": 1.1, "rsi_min": 45, "rsi_max": 78,
               "max_breakout_extension_atr": 1.5, "max_ema_extension_atr": 3.0},
}


def style_of(cfg: StrategyConfig) -> str:
    """'careful', 'active', or 'custom' when the settings match neither."""
    d = cfg.model_dump()
    return next((name for name, vals in STYLES.items() if all(d[k] == v for k, v in vals.items())), "custom")
