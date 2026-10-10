"""Mandatory decision gate.

Every entry order goes through DecisionGate.evaluate(). It runs a fixed list of
checks in order and returns APPROVE, WAIT or REJECT. Only APPROVE carries an
approval token, and the paper execution engine refuses any order without a
valid token. No score, strategy or AI output can skip a check.

Exits (closing a position, stop-loss, take-profit) reduce risk and do not need
gate approval.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.marketdata.models import Ticker

from .config import RiskConfig

APPROVAL_TTL_SECONDS = 30
PAPER_QUOTE = "USDT"


@dataclass
class TradeRequest:
    connection_id: str
    symbol: str
    side: str  # only "buy" opens positions (spot, long only)
    stop_price: Decimal | None  # filled from the strategy's suggestion for strategy orders
    take_profit_price: Decimal | None
    risk_pct: Decimal | None = None
    source: str = "manual"  # manual | strategy
    automatic: bool = False  # placed by the paper autopilot, not by a click


@dataclass
class Check:
    name: str
    label: str
    result: str  # pass | warn | fail | wait | skipped  (only fail and wait block)
    detail: str

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


@dataclass
class GateContext:
    """Everything the gate needs, gathered by the caller. The gate itself does no I/O."""

    connection: dict[str, Any]
    ticker: Ticker | None
    stream_state: str
    account: dict[str, Any] | None
    positions: list[dict[str, Any]]
    marks: dict[str, Decimal]  # symbol -> current bid for open positions
    realized_pnl_today: Decimal
    suspensions: list[dict[str, Any]]
    kill_switch: bool
    pair_min_size: Decimal | None = None
    # Technical signal computed on the server from closed candles (never supplied by the client).
    signal: dict | None = None
    # News assessment from the news engine (app.news.rules.NewsAssessment), computed for this asset and exchange.
    news: Any = None


@dataclass
class Decision:
    id: str
    status: str  # APPROVE | WAIT | REJECT
    summary: str
    checks: list[Check]
    qty: Decimal | None = None
    est_entry_price: Decimal | None = None
    token: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "checks": [c.as_dict() for c in self.checks],
            "qty": str(self.qty) if self.qty is not None else None,
            "est_entry_price": str(self.est_entry_price) if self.est_entry_price is not None else None,
            "inputs": self.inputs,
        }


def equity(ctx: GateContext) -> Decimal:
    cash = ctx.account["cash"] if ctx.account else Decimal(0)
    return cash + sum((p["qty"] * ctx.marks.get(p["symbol"], p["avg_price"]) for p in ctx.positions), Decimal(0))


class DecisionGate:
    def __init__(self, secret: bytes | None = None):
        self._secret = secret or os.urandom(32)

    # --- approval tokens ---------------------------------------------------------------
    def _sign(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        mac = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}|{mac}"

    def verify(self, token: str, connection_id: str, symbol: str, side: str, qty: Decimal) -> str:
        """Return the decision id if the token is valid for exactly this order, else raise."""
        try:
            body, mac = token.rsplit("|", 1)
            payload = json.loads(body)
        except ValueError:
            raise PermissionError("Malformed approval token.") from None
        expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            raise PermissionError("Approval token signature is invalid.")
        if payload["exp"] < time.time():
            raise PermissionError("Approval expired. Market conditions may have changed; request a new decision.")
        if (payload["cid"], payload["sym"], payload["side"], payload["qty"]) != (connection_id, symbol, side, str(qty)):
            raise PermissionError("Approval token does not match this order.")
        return payload["did"]

    # --- evaluation ----------------------------------------------------------------------
    def evaluate(self, req: TradeRequest, ctx: GateContext, cfg: RiskConfig) -> Decision:
        checks: list[Check] = []
        add = lambda name, label, result, detail: checks.append(Check(name, label, result, detail))  # noqa: E731
        conn = ctx.connection
        base, _, quote = req.symbol.partition("-")
        t = ctx.ticker

        # 1. Emergency controls
        add("kill_switch", "Emergency stop", "fail" if ctx.kill_switch else "pass",
            "Emergency stop is on. No new entries." if ctx.kill_switch else "Off.")

        # 2. Connection
        ok_conn = conn["state"] == "PAPER" and conn["health"] == "ok"
        add("connection", "Exchange connection", "pass" if ok_conn else "fail",
            "Paper trading connection is healthy." if ok_conn else
            f"Connection must be in paper mode and healthy (state {conn['state']}, health {conn['health']}).")

        # 3. Pair and side
        if req.side != "buy":
            add("side", "Order type", "fail", "Only buy orders open positions (spot, long only). Use Close to exit.")
        if req.symbol not in conn["selected_pairs"]:
            add("pair", "Trading pair", "fail", f"{req.symbol} is not in this connection's selected pairs.")
        elif quote != PAPER_QUOTE:
            add("pair", "Trading pair", "fail", f"Paper trading currently supports {PAPER_QUOTE} pairs only.")
        else:
            add("pair", "Trading pair", "pass", f"{req.symbol} is enabled.")
        if ctx.account is None:
            add("account", "Paper account", "fail", "Create the paper account first.")

        # 4. Market data
        if t is None:
            add("market_data", "Market data is current", "wait", "No price received yet for this pair.")
        elif ctx.stream_state != "connected" or t.age_seconds() > cfg.max_data_age_seconds:
            add("market_data", "Market data is current", "wait",
                f"Latest price is {t.age_seconds():.1f}s old (limit {cfg.max_data_age_seconds}s), stream {ctx.stream_state}.")
        else:
            add("market_data", "Market data is current", "pass", f"Price is {t.age_seconds():.1f}s old.")

        # 5. Suspensions
        hits = [s for s in ctx.suspensions if (s["scope"], s["target"]) in
                {("global", "*"), ("exchange", conn["exchange"]), ("asset", base)}]
        add("suspensions", "No trading suspension", "fail" if hits else "pass",
            "; ".join(f"{s['scope']} {s['target']}: {s['reason']}" for s in hits) if hits else "None active.")

        # 6. News (safety-only: it can block, delay or shrink an entry, never approve or enlarge one)
        news = ctx.news
        if news is None:
            add("news", "News checked", "warn", "News engine unavailable, so recent news was not checked.")
        else:
            add("news", "News checked", news.result, news.detail)
        news_mult = Decimal(str(max(0.0, min(1.0, news.size_multiplier)))) if news is not None else Decimal(1)

        # 7. Technical setup and over-extension (from closed candles)
        sig = ctx.signal
        if sig is None or sig.get("status") in ("INSUFFICIENT_DATA", "STALE_DATA") or sig.get("over_extended") is None:
            why = "; ".join((sig or {}).get("reasons") or ["No technical analysis available."])
            add("technical", "Technical setup", "wait", why)
            add("extension", "Not over-extended", "wait", "Can't check without current candle data.")
        else:
            failed = [r[2:] for r in sig.get("reasons", []) if r.startswith("✗") and "Over-extended" not in r]
            if sig.get("entry_conditions_met"):
                add("technical", "Technical setup", "pass", f"Trend breakout setup on {sig.get('timeframe')} candles.")
            elif req.source == "strategy":
                add("technical", "Technical setup", "fail", "No strategy setup: " + "; ".join(failed))
            else:
                add("technical", "Technical setup", "warn",
                    "No strategy setup (" + "; ".join(failed) + "). Allowed only because this is a manual paper order.")
            ext = [r.split(": ", 1)[1] for r in sig.get("reasons", []) if r.startswith("✗ Over-extended")]
            add("extension", "Not over-extended", "fail" if sig.get("over_extended") else "pass",
                "Don't chase: " + "; ".join(ext) if sig.get("over_extended") else "Price is within extension limits.")

        # 8. Liquidity
        if t is not None:
            add("liquidity", "Spread and liquidity", "pass" if t.spread_bps <= cfg.max_spread_bps else "wait",
                f"Spread {t.spread_bps:.1f} bps (limit {cfg.max_spread_bps}).")

        qty = entry = None
        if t is not None and ctx.account is not None:
            slip = cfg.slippage_bps / Decimal(10000)
            fee = cfg.taker_fee_rate
            entry = t.ask * (1 + slip)
            cost_per_unit = entry * (1 + fee)
            stop_net = req.stop_price * (1 - slip) * (1 - fee)
            tp_net = req.take_profit_price * (1 - slip) * (1 - fee)

            # 9. Protective exits
            stop_bps = (entry - req.stop_price) / entry * Decimal(10000)
            if req.stop_price <= 0 or req.stop_price >= t.bid:
                add("protective_exit", "Protective stop is valid", "fail", "Stop-loss must be below the current bid.")
            elif stop_bps < cfg.min_stop_distance_bps:
                add("protective_exit", "Protective stop is valid", "fail",
                    f"Stop is only {stop_bps:.1f} bps away (minimum {cfg.min_stop_distance_bps}); normal noise would hit it.")
            elif req.take_profit_price <= entry:
                add("protective_exit", "Protective stop is valid", "fail", "Take-profit must be above the expected entry price.")
            else:
                add("protective_exit", "Protective stop is valid", "pass",
                    f"Stop {stop_bps:.0f} bps below entry; paper engine enforces stop and take-profit.")

            # 10. Net reward:risk
            risk_u = cost_per_unit - stop_net
            reward_u = tp_net - cost_per_unit
            if checks[-1].result == "fail":
                add("reward_risk", "Net reward:risk", "skipped", "Needs a valid stop and take-profit first.")
            elif risk_u > 0 and reward_u > 0:
                rr = reward_u / risk_u
                add("reward_risk", "Net reward:risk", "pass" if rr >= cfg.min_reward_risk else "fail",
                    f"{rr:.2f} after fees and slippage (minimum {cfg.min_reward_risk}).")
            else:
                add("reward_risk", "Net reward:risk", "fail", "No positive net reward after fees and slippage.")

            # 11. Sizing and portfolio limits
            eq = equity(ctx)
            risk_pct = min(req.risk_pct or cfg.max_risk_per_trade_pct, cfg.max_risk_per_trade_pct) * news_mult
            existing = next((p for p in ctx.positions if p["symbol"] == req.symbol), None)
            existing_value = existing["qty"] * ctx.marks.get(req.symbol, existing["avg_price"]) if existing else Decimal(0)
            if risk_u > 0 and eq > 0:
                by_risk = eq * risk_pct / 100 / risk_u
                by_position = max(Decimal(0), eq * cfg.max_position_pct / 100 - existing_value) / cost_per_unit
                by_cash = ctx.account["cash"] / cost_per_unit
                qty = min(by_risk, by_position, by_cash).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                limiter = {by_risk: "risk per trade", by_position: "max position size", by_cash: "available cash"}[min(by_risk, by_position, by_cash)]
                value = qty * entry
                too_small = value < cfg.min_order_value or (ctx.pair_min_size is not None and qty < ctx.pair_min_size)
                add("sizing", "Position size", "fail" if too_small else "pass",
                    f"Size {qty} ≈ {value:.2f} {quote} (limited by {limiter}); below the minimum order." if too_small else
                    f"Size {qty} ≈ {value:.2f} {quote}, losing about {qty * risk_u:.2f} if the stop is hit "
                    f"({qty * risk_u / eq * 100:.2f}% of {eq:.2f} equity; limited by {limiter})."
                    + (f" Risk cut to {news_mult:.0%} because of news." if news_mult < 1 else ""))
            else:
                add("sizing", "Position size", "fail", "Cannot size the position.")

            open_symbols = {p["symbol"] for p in ctx.positions}
            if req.symbol not in open_symbols and len(open_symbols) >= cfg.max_open_positions:
                add("portfolio", "Portfolio limits", "fail", f"Already {len(open_symbols)} open positions (limit {cfg.max_open_positions}).")
            else:
                start = ctx.account["starting_balance"]
                unrealized = sum((p["qty"] * (ctx.marks.get(p["symbol"], p["avg_price"]) - p["avg_price"]) for p in ctx.positions), Decimal(0))
                day_loss = -(ctx.realized_pnl_today + min(unrealized, Decimal(0)))
                limit = start * cfg.daily_loss_limit_pct / 100
                add("portfolio", "Portfolio limits", "fail" if day_loss >= limit else "pass",
                    f"Today's loss {max(day_loss, Decimal(0)):.2f} vs daily limit {limit:.2f}.")

        status = "REJECT" if any(c.result == "fail" for c in checks) else "WAIT" if any(c.result == "wait" for c in checks) else "APPROVE"
        first_bad = next((c for c in checks if c.result in ("fail", "wait")), None)
        summary = "All mandatory checks passed." if status == "APPROVE" else f"{first_bad.label}: {first_bad.detail}"
        decision = Decision(
            id=str(uuid.uuid4()), status=status, summary=summary, checks=checks,
            qty=qty if status == "APPROVE" else None, est_entry_price=entry,
            inputs={
                "symbol": req.symbol, "side": req.side, "source": req.source, "automatic": req.automatic,
                "stop_price": str(req.stop_price), "take_profit_price": str(req.take_profit_price),
                "risk_pct": str(req.risk_pct) if req.risk_pct is not None else None,
                "bid": str(t.bid) if t else None, "ask": str(t.ask) if t else None,
                "ticker_age_s": round(t.age_seconds(), 2) if t else None,
                "risk_config": cfg.as_json(),
                "news": news.as_dict() if news is not None else None,
                "technical_signal": {k: sig.get(k) for k in ("status", "timeframe", "metrics", "reasons", "candle_time_ms")} if sig else None,
            },
        )
        if status == "APPROVE":
            decision.token = self._sign({
                "did": decision.id, "cid": req.connection_id, "sym": req.symbol, "side": req.side,
                "qty": str(qty), "exp": time.time() + APPROVAL_TTL_SECONDS,
            })
        return decision
