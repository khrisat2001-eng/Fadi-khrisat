"""Deterministic news rules: confirmation status and the gate's news assessment.

No model output reaches these rules except the closed-enum fields of a
classification (sentiment, severity, sub-category) and bounded numbers. The
rules can only block, delay or shrink an entry. They never approve anything,
never raise a size and never touch stops or emergency controls.

Everything takes `now` explicitly and only looks at events whose
`available_at` is not later than `now`, so a replay cannot see the future.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import NewsConfig
from .models import (CONFIRMATION_FACTOR, CONFIRMATION_LABELS, CONFIRMED, CREDIBLE, DISPUTED, RUMOR, SENTIMENT_SCORE)

INCIDENT_SUBS = {"security_incident", "exchange_incident", "network_outage", "delisting", "trading_restriction"}
EXCHANGE_WIDE_SUBS = {"security_incident", "exchange_incident"}
HORIZON = {"minutes": timedelta(hours=1), "hours": timedelta(hours=6), "days": timedelta(hours=48), "weeks": timedelta(days=7)}
RANK = {"pass": 0, "warn": 1, "wait": 2, "fail": 3}


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


# --- confirmation ---------------------------------------------------------------------------

def confirm(items: list[dict[str, Any]]) -> tuple[str, str]:
    """items: tier, source_id, hedged, denial, flagged (prompt-injection), direction (-1/0/1)."""
    eff = [(4 if i["flagged"] else i["tier"], i) for i in items]
    denials = [(t, i) for t, i in eff if i["denial"]]
    claims = [(t, i) for t, i in eff if not i["denial"]]
    if not claims:
        return DISPUTED, "Only denials of this report have been seen."
    if any(t <= 2 for t, _ in denials):
        if any(t == 1 for t, _ in denials):
            return DISPUTED, "An official source denies or contradicts it."
        return DISPUTED, "A reputable source disputes it."
    directions = {i["direction"] for t, i in claims if t <= 2 and i["direction"] != 0}
    if len(directions) > 1:
        return DISPUTED, "Reliable sources conflict on what it means."
    firm = [(t, i) for t, i in claims if not i["hedged"]]
    if any(t == 1 for t, _ in firm):
        return CONFIRMED, "Stated by an official (tier 1) source."
    tier2 = {i["source_id"] for t, i in firm if t == 2}
    if len(tier2) >= 2:
        return CONFIRMED, "Reported by two independent reputable sources."
    if tier2:
        return CREDIBLE, "One reputable source; not yet independently confirmed."
    return RUMOR, "Only low-tier sources or hedged wording ('reportedly', 'sources say')."


def confirmation_window(cfg: NewsConfig, timeframe_seconds: int, severity: str, tier: int,
                        spread_bps: float | None = None, volatility_risk: float = 0.0) -> timedelta:
    """How long to wait for confirmation: longer for slow timeframes, weak sources, thin books and
    volatile events; shorter for strong sources. Bounded by the configured min and max."""
    base = cfg.confirmation_candles * timeframe_seconds
    f_sev = {"low": 0.5, "medium": 1.0, "high": 1.5, "critical": 2.0}[severity]
    f_tier = {1: 0.75, 2: 1.0, 3: 1.5, 4: 2.0}.get(tier, 2.0)
    f_liq = 1.5 if spread_bps is not None and spread_bps > 15 else 1.0
    f_vol = 1.0 + 0.5 * max(0.0, min(1.0, volatility_risk))
    secs = base * f_sev * f_tier * f_liq * f_vol
    secs = max(cfg.min_confirmation_minutes * 60, min(cfg.max_confirmation_minutes * 60, secs))
    return timedelta(seconds=secs)


# --- assessment ----------------------------------------------------------------------------

@dataclass
class NewsAssessment:
    result: str  # pass | warn | wait | fail
    detail: str
    size_multiplier: float = 1.0
    events: list[dict[str, Any]] = field(default_factory=list)
    news_score: float = 0.0  # informational only: weight 0 in safety-only mode
    news_confidence: float = 0.0
    scenario: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "news_score": round(self.news_score, 3), "news_confidence": round(self.news_confidence, 3)}


def _scope(ev: dict[str, Any], asset: str, exchange: str) -> str | None:
    how = ev["assets"].get(asset)
    if how in ("name", "ticker"):
        return "asset"
    if exchange in ev["exchanges"] and (not ev["assets"] or ev["subcategory"] in EXCHANGE_WIDE_SUBS):
        return "exchange"
    if ev["market_wide"]:
        return "market"
    return None


def _hhmm(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M UTC")


def assess(events: list[dict[str, Any]], calendar: list[dict[str, Any]], sources: list[dict[str, Any]], cfg: NewsConfig, *,
           asset: str, exchange: str, now: datetime, technical_setup: bool | None, order_source: str,
           timeframe_seconds: int, spread_bps: float | None = None, suspended_event_ids: frozenset[int] = frozenset()) -> NewsAssessment:
    effects: list[tuple[str, float, str, str | None]] = []  # result, multiplier, reason, scenario
    shown: list[dict[str, Any]] = []
    scores: list[tuple[float, float]] = []
    max_age = timedelta(hours=cfg.max_news_age_hours)

    for ev in events:
        avail = ts(ev["available_at"])
        if avail > now or now - avail > max_age:
            continue  # not yet available (point-in-time) or too old to matter for trading
        scope = _scope(ev, asset, exchange)
        if scope is None:
            continue
        age = now - avail
        conf, sev, sub = ev["confirmation"], ev["severity"], ev["subcategory"]
        score = SENTIMENT_SCORE[ev["sentiment"]]
        high = sev in ("high", "critical")
        scoped = scope in ("asset", "exchange")
        window = confirmation_window(cfg, timeframe_seconds, sev, ev.get("best_tier", 4), spread_bps, ev["volatility_risk"])
        label = f"“{ev['title']}” ({CONFIRMATION_LABELS[conf].lower()})"
        result, mult, reason, scen = "pass", 1.0, "", None

        if ev.get("is_repeat_of"):
            reason = f"{label} repeats an older event, so it is not treated as new."
        elif scoped and high and conf == RUMOR:
            scen = "5"
            if age < window:
                result, reason = "wait", f"Scenario 5: unverified report {label}. New entries pause while it is verified (until {_hhmm(avail + window)})."
            else:
                result, mult = "warn", cfg.uncertain_size_multiplier
                reason = f"Scenario 5: {label} is still unverified after the confirmation window; size reduced to {mult:.0%}."
        elif scoped and high and conf == DISPUTED:
            scen, result, mult = "5", "warn", cfg.uncertain_size_multiplier
            reason = f"Scenario 5: conflicting reports {label}; size reduced to {mult:.0%}."
        elif scoped and high and sub in INCIDENT_SUBS:
            scen = "6"
            if ev["id"] in suspended_event_ids:
                result = "warn"
                reason = f"Scenario 6: {label}. A trading suspension was created for it; lifting it is your call."
            elif age < timedelta(hours=cfg.negative_block_hours):
                result = "fail"
                reason = f"Scenario 6: {label}. New entries are blocked until {_hhmm(avail + timedelta(hours=cfg.negative_block_hours))}."
        elif scoped and high and score < 0:
            if age < timedelta(hours=cfg.negative_block_hours):
                scen = "4" if technical_setup else "3"
                result = "fail"
                until = _hhmm(avail + timedelta(hours=cfg.negative_block_hours))
                reason = (f"Scenario 4: price action looks bullish, but {label} blocks new buys until {until}." if technical_setup else
                          f"Scenario 3: {label} blocks new buys until {until}.")
        elif scope == "asset" and score < 0 and sev == "medium":
            result, mult = "warn", cfg.volatile_size_multiplier
            reason = f"Moderately negative news {label}; size reduced to {mult:.0%}."
        elif scope == "asset" and score > 0 and conf in (RUMOR, DISPUTED):
            scen, reason = "5", f"Scenario 5: unverified positive news {label} is ignored."
        elif scope == "asset" and score > 0 and sev != "low":
            if technical_setup:
                scen = "1"
                reason = f"Scenario 1: positive news {label} with technical confirmation. News does not raise the size (safety-only mode)."
            elif age < window:
                scen, result = "2", "wait"
                reason = f"Scenario 2: positive news {label} without price confirmation. Waiting for a setup until {_hhmm(avail + window)}."
            else:
                scen, result = "2", "fail" if order_source == "strategy" else "warn"
                reason = f"Scenario 2: positive news {label} was never confirmed by price action. Don't buy the headline."

        # Volatility overlay: any event that raises volatility shrinks size while its impact lasts.
        if ev["volatility_risk"] >= 0.6 and age < HORIZON[ev["horizon"]] and not ev.get("is_repeat_of") and conf != DISPUTED:
            if RANK[result] <= RANK["warn"]:
                if reason:
                    reason += f" It may also raise volatility; size reduced to {cfg.volatile_size_multiplier:.0%}."
                else:
                    reason = f"{label} may raise volatility; size reduced to {cfg.volatile_size_multiplier:.0%}."
                result = "warn"
            mult = min(mult, cfg.volatile_size_multiplier)

        if scope == "market" and result == "pass":
            continue
        if result != "pass" or reason:
            effects.append((result, mult, reason or f"{label}: no effect.", scen))
        url = ev["items"][0]["url"] if ev.get("items") else None
        shown.append({"id": ev["id"], "title": ev["title"], "url": url, "source": ev.get("best_source"),
                      "published_at": ev["items"][0]["published_at"] if ev.get("items") else None,
                      "available_at": ev["available_at"], "confirmation": conf, "sentiment": ev["sentiment"],
                      "severity": sev, "scope": scope, "effect": result, "reason": reason or "No effect."})
        if scope == "asset" and conf in (CONFIRMED, CREDIBLE) and not ev.get("is_repeat_of"):
            scores.append((score * CONFIRMATION_FACTOR[conf] * ev["relevance"] * ev["novelty"], ev["confidence"]))

    # Scheduled events: no-trade windows.
    for c in calendar:
        if c["status"] in ("cancelled", "completed"):
            continue
        if c["assets"] and asset not in c["assets"]:
            continue
        w = cfg.no_trade_windows.get(c["impact"])
        at = ts(c["scheduled_at"])
        if w is None or (w.before_minutes == 0 and w.after_minutes == 0):
            continue
        start, end = at - timedelta(minutes=w.before_minutes), at + timedelta(minutes=w.after_minutes)
        if start <= now <= end:
            unsure = " Its time is not verified, so the window is applied anyway." if c.get("uncertain") else ""
            effects.append(("wait", 1.0, f"No-trade window for {c['name']} ({c['impact']} impact) at {_hhmm(at)}, until {_hhmm(end)}.{unsure}", None))

    # Feed health.
    enabled = [s for s in sources if s["enabled"]]
    oks = [ts(s["last_ok_at"]) for s in enabled if s.get("last_ok_at")]
    if not enabled:
        effects.append(("warn", 1.0, "News monitoring is off (no sources enabled), so news risks are not checked.", None))
    elif not oks or now - max(oks) > timedelta(minutes=cfg.feeds_stale_minutes):
        since = f"since {_hhmm(max(oks))}" if oks else "yet"
        effects.append(("wait" if cfg.block_when_feeds_stale else "warn", 1.0,
                        f"News feeds have not updated {since}, so recent news may be missing.", None))

    if not effects:
        return NewsAssessment("pass", f"No relevant news in the last {cfg.max_news_age_hours}h; feeds are current.", events=shown)
    worst = max(RANK[e[0]] for e in effects)
    result = next(k for k, v in RANK.items() if v == worst)
    ordered = sorted(effects, key=lambda e: -RANK[e[0]])
    reasons = [e[2] for e in ordered if e[0] != "pass"] or [e[2] for e in ordered]
    detail = " ".join(reasons[:3]) + (f" (+{len(reasons) - 3} more)" if len(reasons) > 3 else "")
    mult = max(0.0, min(1.0, min(e[1] for e in effects)))
    scenario = next((e[3] for e in ordered if e[3]), None)
    n = sum(s for s, _ in scores) / len(scores) if scores else 0.0
    c = sum(x for _, x in scores) / len(scores) if scores else 0.0
    return NewsAssessment(result, detail, mult, shown, n, c, scenario)
