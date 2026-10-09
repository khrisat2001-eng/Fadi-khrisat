"""Risk settings. Stored in the database; defaults below are conservative."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class RiskConfig(BaseModel):
    max_data_age_seconds: float = Field(10, gt=0, le=120, description="Ticker older than this blocks new entries")
    max_spread_bps: Decimal = Field(Decimal("30"), gt=0, le=500, description="Wider bid/ask spread blocks new entries")
    taker_fee_rate: Decimal = Field(Decimal("0.001"), ge=0, le=Decimal("0.01"), description="Fee per side, e.g. 0.001 = 0.1%")
    slippage_bps: Decimal = Field(Decimal("5"), ge=0, le=200, description="Assumed slippage per fill")
    max_risk_per_trade_pct: Decimal = Field(Decimal("1"), gt=0, le=5, description="Max loss to stop, as % of paper equity")
    max_position_pct: Decimal = Field(Decimal("20"), gt=0, le=100, description="Max size of one asset, as % of equity")
    max_open_positions: int = Field(5, ge=1, le=50)
    daily_loss_limit_pct: Decimal = Field(Decimal("3"), gt=0, le=50, description="New entries stop for the day after this loss")
    min_reward_risk: Decimal = Field(Decimal("1.5"), gt=0, le=20, description="Net reward:risk after fees and slippage")
    min_stop_distance_bps: Decimal = Field(Decimal("10"), ge=0, le=5000, description="Stops closer than this are rejected as noise")
    min_order_value: Decimal = Field(Decimal("5"), gt=0, description="Smallest order in quote currency")

    def as_json(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in self.model_dump().items()}
