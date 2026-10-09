# Exchange Portal and AI News Engine: Design and Build Plan

Written 2026-10-09 for Fadi's "Additional requirements" message. Status: draft for review. No code has been written yet.

## 0. Starting point

- No trading app exists in the repositories this project can see. `khrisat2001-eng/Fadi-khrisat` is empty (no commits) and `khrisat2001-eng/Deliver-App` is a taxi app.
- The requirements refer to an "existing technical analysis and strategy selection engine". That engine is not in any visible repo, so this plan treats it as a dependency that must either be located or built (Phase 2).
- Live trading is out of scope for every phase below. The plan builds everything up to, and including, paper trading and the evidence needed for a later live-trading decision.

### Suggested stack (default if no app exists)

| Layer | Choice | Why |
|---|---|---|
| Backend API | Python 3.12 + FastAPI | Best ecosystem for quant work (pandas, numpy, TA libs) and for LLM calls |
| Workers | Separate Python processes (market data, news ingest, classifier, decision engine) using a Redis-backed queue | Isolates failures; a news outage never stalls risk monitoring |
| Database | PostgreSQL (+ TimescaleDB extension for candles) | Relational audit trail plus time-series |
| Cache / pub-sub | Redis | Rate-limit buckets, live state, WebSocket fan-out |
| Frontend | Next.js (React, TypeScript) | Wizard and dashboard; talks only to our backend, never to exchanges |
| Secrets | Cloud KMS (AWS KMS / GCP KMS) or HashiCorp Vault for the master key | Envelope encryption of exchange credentials |
| LLM | Claude via the Anthropic API, structured JSON output, no tools | Classification only; never decides trades |

If Fadi already has an app in a different stack, the architecture below still applies; only the language changes.

---

## 1. Exchange Connections Portal

### 1.1 Connector architecture (modular)

One interface, one adapter per exchange. Adding Binance or Bybit later means writing one new adapter and one wizard content file, nothing else.

```
ExchangeConnector (interface)
  describe()            -> name, credential fields, permission model, docs/steps for wizard
  validate_credentials()-> read-only call; returns permissions, account id, IP-binding status
  fetch_balances()
  fetch_capabilities()  -> tradable pairs, min sizes, tick sizes, fee tier
  market_stream()       -> public WebSocket (tickers, trades, order book, candles)
  account_stream()      -> private WebSocket (orders, fills, balances)
  place_order() / cancel_order() / fetch_open_orders()   # live adapter only, locked
  rate_limiter          -> per-endpoint token buckets, configured per exchange
  error_map             -> exchange error code -> plain-language message + category

Implementations: OkxConnector, KucoinConnector
PaperConnector: wraps any connector; real market data, simulated fills (fees + slippage model)
```

Design rules:
- The order-placing methods live behind an `ExecutionGateway` that refuses every call unless the connection is in `LIVE_AUTHORIZED` state **and** the order carries a decision-gate approval token (Section 7). In this project's scope the gateway is wired only to `PaperConnector`.
- Use the `ccxt` library only as a reference or for public market data if convenient; the authenticated adapters should be thin, explicit code so permission checks and error mapping are fully under our control.
- No endpoint URLs or keys are ever typed by the user. Endpoints are constants in each adapter.

### 1.2 Exchange-specific facts the wizard must explain

These are from each exchange's public API documentation as I understand it. **Each item must be re-checked against the live docs when the adapter is built**, because exchanges change these rules.

**OKX (API v5)**
- Credentials: API key, secret key, passphrase (the passphrase is chosen by the user when creating the key).
- Permissions offered: Read, Trade, Withdraw. Wizard asks for **Read only** at first.
- IP allowlist supported, recommended. OKX documentation states that keys with trade or withdraw permission and no bound IP are removed after a period of inactivity; the wizard should warn about this.
- Read-only validation calls: account configuration (returns the key's permission level and account mode) and account balance.
- OKX has a separate demo-trading environment with its own keys. We can still use our own paper engine for consistency across exchanges.
- Region note: OKX is not available to residents of some countries. The wizard should link to OKX's own eligibility page rather than make claims.

**KuCoin**
- Credentials: API key, secret, passphrase. Current API key version signs the passphrase with the secret (version header "2").
- Permissions offered: General (read), Spot trading, Margin, Futures, Transfer, Withdrawal. Wizard asks for **General only** at first.
- IP restriction supported, recommended.
- Read-only validation calls: list accounts; the API-key-info endpoint (if available on the account type) to read back the permission list.
- KuCoin's old sandbox is not a dependable paper-trading option, so paper trading for KuCoin uses our own simulator on real market data.
- WebSocket requires first requesting a connection token over REST; the token response gives the ping interval.

### 1.3 Connection wizard (10 steps mapped to screens)

| Step | Screen | Backend action |
|---|---|---|
| 1 | Pick OKX or KuCoin (cards with logo and status) | none |
| 2 | "Create your API key" with numbered steps and screenshots per exchange | content from `describe()` |
| 3 | Permission explainer: what to tick, what never to tick (Withdraw, Transfer), IP allowlist with our server's egress IPs shown | none |
| 4 | Secure form: key, secret, passphrase. Password-type inputs, no autofill, no query string, POST over HTTPS to our backend | encrypt immediately, store as `PENDING` |
| 5 | "Testing your key…" spinner | `validate_credentials()` read-only |
| 6 | Balances and capabilities | `fetch_balances()`, `fetch_capabilities()` |
| 7 | Result: green/amber/red with plain-language issues ("This key can withdraw funds. Please create a new key without Withdraw.") | permission audit |
| 8 | Allocation: max % or amount of balance the bot may use; pick pairs from the supported list | save to `connection_settings` |
| 9 | Enable market data + paper trading | state -> `PAPER` |
| 10 | Live trading: shown as locked, with the checklist of what must pass first | no action in this scope |

Validation outcomes and what we do:

| Finding | Action |
|---|---|
| Withdraw permission enabled | **Reject the key.** Delete the stored ciphertext. Ask for a new key. |
| Transfer permission enabled | Reject (we never move funds between accounts). |
| Trade permission enabled during initial setup | Accept for read and paper only, show amber warning recommending a read-only key until live trading is approved. |
| No IP restriction | Amber warning with steps to add our IPs. |
| Invalid signature / wrong passphrase | Red, plain message: "The secret or passphrase doesn't match this key." |
| Clock skew error | Handled server-side (we sync time), never shown as user error unless persistent. |

### 1.4 Connection management page

One card per exchange, independent controls. Each card shows:
- Status badge: Connected / Degraded / Auth failed / Disconnected.
- Mode badge: **PAPER** (blue) or **LIVE** (red, always visible on every page when any connection is live).
- Key shown as `••••••••3f9a` (last 4 only), permissions detected, IP-bound yes/no.
- Last successful sync time, balances, REST and WebSocket health.
- Buttons: Test connection, Sync balances, Replace key, Disconnect.

Background jobs:
- **Health check** every few minutes: a cheap authenticated read. Auth errors flip the card to "Auth failed", pause all activity on that connection, and send an alert.
- **WebSocket supervisor**: heartbeat/ping per exchange spec, reconnect with exponential backoff plus jitter, resubscribe, then reconcile state over REST (orders and balances) after each reconnect. Data older than a configured age marks market data as stale, which the decision gate treats as a failed check.
- **Rate limiting**: per-endpoint token buckets from each exchange's published limits, plus reading any rate-limit headers the exchange returns. On a 429-type error: back off, never retry-storm, surface "Exchange is limiting requests; retrying" only if it persists.

Safe disconnect: stop new activity, cancel nothing automatically on live accounts (out of scope anyway), revoke our stored ciphertext (crypto-shred the data key), keep the audit log. Replace key: validate the new key fully before swapping, then shred the old one.

### 1.5 Credential security

- **Envelope encryption**: each credential set gets its own random data key; secret, passphrase and key are encrypted with AES-256-GCM; the data key is encrypted by the KMS master key. Associated data binds the ciphertext to `user_id + exchange + connection_id` so a row cannot be swapped to another user.
- Decryption happens only inside the worker that signs requests, in memory, for the signing call.
- API responses never include secrets, ever. The frontend receives masked key, permissions and status only.
- Logging: request bodies of the credential endpoint are never logged; a redaction filter strips anything that looks like a key/secret/passphrase from all logs and error reports.
- The UI and docs state clearly: never paste keys into chat, email, source code or URLs. The only entry point is the wizard form.
- No automatic fund transfers between exchanges exist anywhere in the codebase (no transfer or withdrawal methods are implemented in the adapters at all).
- Live trading can never be switched on by connecting a key, by an API call from the frontend alone, or by the AI. It needs the explicit authorization flow in Phase 6, which is outside this plan's build scope.

### 1.6 Connection state machine

```
NEW -> PENDING_VALIDATION -> CONNECTED_READONLY -> PAPER
                         \-> REJECTED (bad perms / invalid)
PAPER -> AUTH_FAILED (auto, on 401-type errors) -> PAPER (after Test passes)
PAPER -> LIVE_ARMED -> LIVE_AUTHORIZED   (locked; requires Section 9 acceptance + typed user confirmation + re-auth)
any -> DISCONNECTED (user)
```

---

## 2. AI News Intelligence Engine

### 2.1 Pipeline

```
Sources -> Ingest -> Normalize -> Deduplicate & cluster into Events -> Asset mapping
       -> Verification (source tier, corroboration) -> LLM classification (facts vs interpretation)
       -> Market reaction check (deterministic, from our market data)
       -> News signal + safety triggers -> Decision gate / Risk engine / Dashboard
```

Each stage is a separate worker reading from and writing to the database, so any stage can fail or lag without corrupting others, and every step is auditable.

### 2.2 Sources (candidates; licensing must be confirmed before use)

I have not verified current pricing or terms for any paid provider. Treat this list as a shortlist to check, not as facts.

| Category | Candidate sources | Access type |
|---|---|---|
| Exchange announcements | OKX announcements API, KuCoin announcements API | Official API |
| Project / protocol | Official project blogs (RSS), GitHub release feeds of major protocols, official X accounts (low tier unless mirrored on official site) | RSS / Atom |
| Regulatory | SEC press releases and EDGAR filings (RSS), CFTC press releases, ESMA, UK FCA, other regulators relevant to Fadi's jurisdiction | RSS / official |
| Macro calendar | Federal Reserve FOMC calendar and statements, BLS release schedule (CPI, jobs), BEA (GDP, PCE), ECB | Official RSS / published schedules |
| Crypto news aggregation | CryptoPanic, Messari, CoinDesk, The Block, others | Usually paid API or licensed RSS |
| Token unlocks | Tokenomist (formerly Token Unlocks) or similar | Paid |
| Market data signals (volume, funding, liquidations) | Our own OKX/KuCoin feeds first; Coinglass / Kaiko / Glassnode if licensed | Exchange API / paid |
| Social | Treated as lowest tier; only for "rumor detected, verify" alerts | Licensed API only |

Rules: source allowlist stored in the database with a reliability tier (1 = official/primary, 2 = reputable outlet, 3 = aggregator, 4 = social/unknown). Unknown sources default to tier 4. Scraping sites whose terms forbid it is not allowed.

### 2.3 Data model (core tables)

- `news_item`: id, source_id, url, canonical_url, headline, body_excerpt, published_at (from source), **first_seen_at** (our ingest time), language, raw_hash, fetch_status.
- `event`: id, title, category (A–D sub-category), event_time (if known), assets[], exchanges[], confirmation_status, first_seen_at, is_repeat_of_event_id, cluster of news_item ids.
- `classification`: event_id, model_version, prompt_version, stated_facts[] (each with quote + news_item_id), interpretation (text), sentiment (6-level enum), severity, credibility, relevance per asset, novelty, impact_horizon, volatility_risk, confidence, created_at.
- `market_reaction`: event_id, asset, window, price_change, volume_vs_avg, spread_change, atr_change, sustained_or_reversed, computed_at.
- `news_signal`: asset, event_id, direction score, weight, safety_actions[] (block entries, reduce size, suspend), valid_until.
- `decision_log`: every trade decision with all inputs (Section 3.4).

Nothing in these tables is ever generated without a source row. A classification with no linked news_item is invalid by schema.

### 2.4 Dedup and novelty

1. Exact: canonical URL and normalized headline hash.
2. Near-duplicate: text embeddings or MinHash; items above a similarity threshold within a time window join the same `event`.
3. Repeat detection: when a new cluster matches an event older than N hours (configurable), mark `is_repeat_of`; novelty score drops and it cannot create a new catalyst signal.

### 2.5 Asset mapping

- Asset registry with symbol, name, aliases, contract addresses, chains and exchange tickers.
- Ambiguous tickers and generic words (e.g. tokens named after common words) require name or contract match, not just ticker match.
- The LLM proposes assets; the code accepts only assets in the registry and drops the rest. Low-confidence mappings are flagged "attribution uncertain" and cannot produce a trading signal.

### 2.6 Verification and confirmation status

Deterministic rules, not LLM opinion:

| Status | Rule |
|---|---|
| Confirmed | Tier 1 source states it, or two independent tier 2 sources with the same facts |
| Credible, unconfirmed | One tier 2 source |
| Rumor / speculative | Tier 3–4 only, or hedged language ("reportedly", "sources say") |
| Disputed / likely false | An official source contradicts it, or sources conflict on the core fact |

Status is recalculated every time a new item joins the event. Conflicts lower confidence and set the "Uncertain / Conflicting" sentiment.

### 2.7 LLM classification (and prompt-injection defense)

- Model gets: the article text wrapped clearly as untrusted data, the event metadata, and a fixed instruction. **It has no tools, no access to credentials, settings, or trading functions.** Its only output is JSON matching a strict schema; anything that fails validation is discarded and retried once, then marked "unclassified".
- Output separates `stated_facts` (each must quote a span from the source text, which the code checks exists verbatim) from `interpretation`. Facts without a matching quote are dropped, which catches invented facts.
- Enums only for sentiment, severity, horizon. Numeric fields are bounded 0–1.
- Content that looks like instructions ("ignore previous", "set risk to", URLs to execute) is flagged as a manipulation signal and lowers credibility; it can never change behavior because the model output can only populate the classification row.
- Store model id and prompt version with each row so results are reproducible and comparable.
- Model choice: a fast, cheap Claude model for bulk classification, a stronger Claude model only for high-severity events. Final model ids to be picked at build time.

### 2.8 Market reaction check (deterministic)

For each event and affected asset, from our own market data:
- Price change since first_seen_at over several windows (e.g. 5m, 15m, 1h, 4h, scaled to the strategy timeframe).
- Volume vs trailing average, spread and top-of-book depth change, ATR change.
- Breakout/breakdown relative to recent support/resistance.
- Sustained vs reversed (did the move hold over the confirmation window).
- "Already priced in" heuristic: a large move before our first_seen_at means the news is not fresh for trading purposes.

The LLM may suggest explanations for a mismatch (positive news, falling price), but those are stored as hypotheses and do not feed the score.

### 2.9 Scheduled events calendar

- Built only from sources with a stated date and time (official calendars, exchange maintenance notices, project announcements). Each entry stores source URL and last verified time.
- Status: Scheduled, Confirmed, Changed, Completed, Cancelled, **Uncertain** (time unverified or source stale beyond a configured age).
- Times stored in UTC, shown in the user's timezone.
- Per-event no-trade windows: user sets minutes before and after for each impact level (defaults suggested, e.g. FOMC and CPI: 30 min before, 30 min after for short timeframes).
- The system never predicts an event's outcome in the calendar.

---

## 3. Combining News with Technical Analysis

### 3.1 Principle

**News can only make the system more careful until testing proves otherwise.** In the first integration (Phase 4), news may block entries, reduce size, require stronger confirmation, or suspend trading. It may not raise position size, raise trade frequency, or bypass any limit. A positive news contribution to scores is allowed only after Phase 5 shows a robust improvement after fees.

### 3.2 Scoring model (configurable, visible)

```
technical_score  T  in [-1, 1]   from the strategy engine (trend, S/R, momentum, volume)
regime_score     R  in [-1, 1]   volatility, liquidity, BTC/market trend, macro window
news_score       N  in [-1, 1]   sentiment * credibility * relevance * novelty * confirmation_factor
news_confidence  c  in [0, 1]

combined = wT*T + wR*R + wN*c*N          (weights in config, default wN small, e.g. 0.15)
```

- `confirmation_factor`: 1.0 confirmed, 0.5 credible, 0.0 rumor, 0.0 disputed (rumors never move the score; they can only trigger a "verify" alert or a temporary entry block).
- The dashboard shows each component's contribution as a bar so the user sees why.

### 3.3 Confirmation matrix (implemented as rules, evaluated before scores)

| Scenario | News | Price/technicals | Outcome |
|---|---|---|---|
| 1 | Positive, verified | Bullish confirmed | Buy allowed only if every gate check passes |
| 2 | Positive | Weak or bearish | WAIT for confirmation window; REJECT if it expires unconfirmed |
| 3 | Negative, verified | Bearish confirmed | Block new buys; flag open positions for the strategy's own exit rules |
| 4 | Negative | Bullish | No auto sell/short; keep monitoring; check position still within limits |
| 5 | Unverified or conflicting | any | Reduce news weight to 0, require stronger technicals, or NO TRADE |
| 6 | Security incident / exchange disruption | any | Suspend new entries on affected asset or exchange immediately; emergency policy for open positions |

### 3.4 Adaptive confirmation window

`window = base(timeframe) * f(severity) * f(liquidity) * f(volatility) * f(source tier)`, bounded by min and max per timeframe. Example: a 15m strategy might need 2–4 closed candles with volume above average; a 4h swing strategy might need 1–2 closed 4h candles. Scheduled events use the calendar windows instead. Critical events skip the window and apply safety actions immediately.

### 3.5 Exhaustion ("don't chase") filter

Reject or delay a long entry if any configured threshold trips:
- Price more than X × ATR above the breakout level or the recent range.
- Price more than Y% above the 20 or 50 period moving average.
- RSI above a set level together with falling momentum or volume.
- Remaining upside to the next resistance divided by stop distance below the minimum reward-to-risk.
- Spread or estimated slippage above limit.

Result is WAIT (for pullback or new setup) or REJECT, with the tripped metric shown.

---

## 4. Risk Controls (independent of news and AI)

The risk engine is a separate module with its own config. News produces requests; the risk engine decides.

- **Suspension registry**: entries keyed by asset, exchange, or global, each with reason, source event, start, and review time. Blocks new entries only.
- **Volatility scaling**: position size = risk budget / stop distance, with stop distance from current ATR. Higher volatility means smaller size, never larger risk.
- **Stops are never widened automatically.** A recalculated stop can only tighten or stay. Changing a protective order requires the replacement to be confirmed live before the old one is cancelled (paper engine simulates this the same way).
- **Portfolio limits** (max exposure per asset, per exchange, total, daily loss limit, max open positions) are checked last and cannot be overridden by any signal score.
- During suspensions, open positions and their protective orders keep being monitored.
- Emergency controls (kill switch, daily loss stop) are outside the news engine's reach; no code path from news to these exists except "raise alert".
- Alerts when an event materially changes risk on an open position (in-app, plus email/Telegram later).

---

## 5. Mandatory Decision Gate

A single deterministic function. Every order request, paper or live, must pass through it and receive a signed approval token that the execution gateway checks. Nothing (AI output, high score, strategy code) can call execution without the token.

Checks, in order. Any failure returns REJECT or WAIT with the reason:

1. Market data fresh (last tick and candle within max age) and WebSocket healthy.
2. Connection healthy and in an allowed state.
3. No suspension applies to this asset or exchange; not inside a no-trade window.
4. News inputs verified enough for their weight (unverified news has zero weight).
5. Market reaction evaluated for any relevant event in the lookback.
6. Technical setup satisfies the chosen strategy's entry rules.
7. Not over-extended (Section 3.5).
8. Liquidity: spread and depth sufficient for the order size.
9. Expected net return after fees and slippage above minimum; reward-to-risk above minimum.
10. Position size and portfolio limits satisfied.
11. Protective exit can be placed (supported order type, stop price valid).

Outputs: APPROVE, WAIT (with re-check condition), REJECT, NO TRADE. Each decision is written to `decision_log` with every input value, the news event ids and links, the score breakdown and the failed check if any.

---

## 6. News Intelligence Dashboard

Pages and widgets:
- **Feed**: latest events (not raw articles), each with headline, source and link, published time and our detected time, assets, category, sentiment badge, credibility tier, confirmation status, impact and horizon, "repeat" tag.
- **By asset**: per-coin view grouping events by category with a sentiment strip over time and the price chart with event markers.
- **Calendar**: upcoming events in user's timezone with status and configured no-trade windows (editable).
- **Signals vs technicals**: per asset, the T / R / N bars and the current gate result.
- **Decisions**: list of approved, rejected, delayed decisions; clicking one shows every check, which news items influenced it and how (with links).
- **Alerts**: suspensions, auth failures, connectivity, risk-change alerts.
- Filters: asset, exchange, source, category, severity, date range, confirmation status.

---

## 7. Testing and Acceptance

### 7.1 Point-in-time backtesting

- Every news item carries `available_at = max(published_at, first_seen_at)` for live data. For historical data, use the provider's own first-published time and add a configurable detection delay.
- The backtest engine only gives the strategy news with `available_at <= current bar time`. A unit test inserts a future article and asserts it is invisible.
- **Limitation to document up front:** good historical, timestamped crypto news archives are scarce and usually paid. If we don't have one, the news strategy can be tested only from the day our own ingestion starts, through paper trading, and the report must say the news component is not fully validated.

### 7.2 Test matrix

| Area | Tests |
|---|---|
| Performance | Technical-only vs news-only (research) vs combined; net of fees and slippage; drawdown; performance in major event windows; walk-forward and out-of-sample splits |
| News quality | False positive/negative rate of signals against labelled events; latency publish -> detect -> decide |
| Robustness | Conflicting reports, unverified reports, feed outage, delayed or stale market data, duplicated articles, wrong-asset attribution, prompt-injection articles |
| Safety (must all pass) | News cannot raise size; cannot bypass portfolio limits; cannot widen stops; cannot disable emergency controls; no execution without gate token; suspended asset cannot be bought |
| Exchange | Credential validation for each permission combo (with recorded API fixtures, no real keys in tests), withdraw-enabled key rejected, auth failure detection, WebSocket reconnect + reconcile, rate-limit backoff |
| Security | Secrets never in API responses or logs (automated scan), ciphertext bound to user, frontend has no exchange-signing code |

### 7.3 Acceptance criteria before any live-trading discussion

- All safety tests pass in CI.
- At least N weeks (Fadi to choose) of paper trading on both exchanges with no unexplained decision, no stale-data trade and no risk-limit breach.
- Combined strategy beats or matches technical-only after fees on out-of-sample data, or the news engine stays in "safety-only" mode.
- Written review of results, including the news-data limitation if it applies.

---

## 8. Phased Build Plan

| Phase | Delivers | Exit check |
|---|---|---|
| 0. Foundation | Repo, stack, CI, DB schema, secret management (KMS), auth for the web app, logging with redaction | CI green; secrets scan clean |
| 1. Exchange portal (read-only) | Connector interface, OKX and KuCoin adapters (read endpoints, public + private WebSockets), wizard, management page, health checks, alerts | Real read-only keys connect, show balances, withdraw-enabled key is rejected, reconnect works |
| 2. Paper trading core | Paper connector (fills, fees, slippage), risk engine, decision gate skeleton, technical strategy engine (or integration with Fadi's existing one), decision log | Paper trades flow end to end through the gate with full logs |
| 3. News engine (research mode) | Ingest from confirmed sources, dedup, asset mapping, verification rules, LLM classification, market reaction, calendar, dashboard | News visible and auditable; no effect on trading yet |
| 4. News in safety-only mode | Suspensions, no-trade windows, entry blocks, size reductions, exhaustion filter, confirmation matrix | Safety test suite passes; news never increases risk |
| 5. Evaluation | Point-in-time backtests, paper-trading comparison, reports | Acceptance criteria in 7.3 met, or documented as not met |
| 6. Live authorization (not in this scope) | Locked live flow with explicit user confirmation | Separate decision by Fadi |

Phases 1 and 3 can run in parallel once Phase 0 is done.

---

## 9. Open questions for Fadi

1. Where is the existing app, and what language/framework is it in? If there is none, is the stack in Section 0 OK?
2. Is there an existing technical strategy engine to integrate with, or should Phase 2 build one?
3. Which country are you trading from? This affects OKX/KuCoin eligibility and which regulators to watch.
4. Budget for paid data (news API, token unlocks, historical news archive)?
5. Where will the server run (cloud provider), so we know which KMS and fixed egress IPs to use for the exchange IP allowlist?
6. Single user (you only) or multiple users?
