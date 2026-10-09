# Fadi Trading Platform

Crypto trading platform for OKX and KuCoin. Live trading is disabled.

## What's here so far

**Exchange Connections Portal** (Phase 1 of [docs/design-plan.md](docs/design-plan.md)):

- Guided 10-step wizard for connecting OKX and KuCoin with read-only API keys.
- Keys are validated with a read-only request. Keys with withdrawal or transfer permission are rejected and deleted.
- Credentials are stored with envelope encryption (AES-256-GCM, a data key per connection, bound to that connection). The browser only ever sees the last 4 characters of the key.
- Connection management: status, health, balances, last sync, test, sync, replace key, disconnect, alerts for authentication failures and connectivity problems, rate limiting with backoff, clock resync.
- Paper trading can be switched on per connection. Live trading is locked and the API refuses it.

**Market data and paper trading** (Phase 2):

- Live ticker streams from OKX and KuCoin public WebSockets for the selected pairs, with heartbeats, silence detection, reconnect with backoff, and alerts when a stream drops.
- Paper trading accounts in USDT. Fills use the live bid/ask plus configured slippage and fees.
- A mandatory decision gate checks every entry: emergency stop, connection health, pair, fresh market data, suspensions, spread, a valid stop-loss and take-profit, net reward:risk after costs, position sizing, open-position count and the daily loss limit. Only approved orders get a short-lived signed token, and the engine refuses orders without one. Every decision is logged with all of its checks.
- Stop-loss and take-profit are enforced on every price update. Stops can be tightened but never widened.
- Risk controls: emergency stop, trading suspensions by asset, exchange or globally, and editable risk settings.

**Technical strategy** (Phase 2b):

- Closed candles from OKX and KuCoin (5m, 15m, 1h or 4h), cached.
- Trend breakout strategy: uptrend (EMA20 above EMA50, close above EMA50), close above the 20-candle resistance, breakout volume at least 1.5× average, RSI 50-75. It suggests a stop at 2 ATR and a target at 6 ATR. All settings are editable.
- "Don't chase" filter: blocks any entry, manual or strategy, when the price is more than 1 ATR above the breakout level, more than 2.5 ATR above EMA20, RSI is overheated, or momentum is fading on lower volume.
- Signals are computed on the server and feed the decision gate's technical and extension checks. Missing or stale candles mean WAIT. Strategy orders need a setup; manual paper orders without one get a warning.

**News intelligence** (Phases 3 and 4, safety-only):

- Reads only allowlisted sources, each with a reliability tier: OKX and KuCoin announcement APIs, SEC and Federal Reserve press releases, the Ethereum Foundation blog and Bitcoin Core releases (tier 1, on). CoinDesk and Cointelegraph (tier 2) ship switched off until you've checked their feed terms. Fetching is HTTPS-only with time and size limits, and XML is parsed with `defusedxml`.
- Every item keeps its link, the source's publication time and the time we first saw it. Items are deduplicated by canonical URL and headline, grouped into events, and old stories that come back are marked as repeats so they can't act as a new catalyst.
- Assets are attributed from a registry. Tickers that are ordinary words (LINK, DOT, NEAR...) need a name, a `$TICKER` or a pair.
- Confirmation status is decided by rules, not by AI: confirmed (official source, or two independent reputable outlets), credible, rumor, or disputed (denied by an official or reputable source, or sources conflict).
- Classification into the four categories and six sentiment levels, plus severity, horizon, relevance, volatility risk and confidence. With `ANTHROPIC_API_KEY` set it uses Claude with structured outputs: the article is wrapped as untrusted data, the model has no tools, every stated fact must quote the article word for word or it is dropped, and text aimed at automated readers is flagged and downgraded. Without a key, or if the call fails, simple keyword rules are used and labelled as such.
- Market reaction: the price when we first saw the news versus now, from our own feeds.
- Scheduled events calendar with per-impact no-trade windows. Each entry needs a source link; unverified or stale times are shown as uncertain.
- The gate's news check applies the confirmation matrix: unverified critical reports pause entries for an adaptive confirmation window; credible security incidents and exchange disruptions create a suspension; confirmed serious negative news blocks buys for a set time; positive news without a technical setup waits for price confirmation; uncertain or volatile news cuts the risk per trade. News never approves a trade, raises a size, widens a stop or turns off emergency controls.
- Material news on an asset you hold raises an alert. The stop stays where it is and nothing is sold automatically.
- Dashboard: news vs technicals by pair, a filterable feed with source links and facts kept apart from interpretation, the calendar, what news has done, source health, settings, and paper results split by whether news was in play.

Data limitations: the source URLs above could not be reached from the build environment, so the parsers are tested against recorded-shape fixtures and need a check on a real deployment. There is no historical, timestamped news archive, so the news rules have not been backtested. Every item stores `available_at` so a point-in-time replay is possible once an archive or enough of our own history exists.

Not built yet: automatic strategy execution (signals are acted on with one click) and backtesting. See the plan for the phases.

## Run locally

```bash
pip install -e ".[dev]"
cp .env.example .env   # then fill in CREDENTIAL_MASTER_KEY and APP_ACCESS_TOKEN
set -a; source .env; set +a
uvicorn app.main:app --port 8000      # or: fastapi run app/main.py
```

Hosts that look for an `app` in `app/main.py` find it there. If `APP_ACCESS_TOKEN` or `CREDENTIAL_MASTER_KEY` is missing, the app still starts but only answers "Setup needed" with what to set.

Open http://localhost:8000 and sign in with `APP_ACCESS_TOKEN`. For local http, set `SECURE_COOKIES=false`. In production, run it behind HTTPS only.

Never paste exchange API keys into chat, email, code or URLs. The wizard form is the only place to enter them.

## Deploy

The app needs an always-on server with a persistent disk (live price streams, stop checks and news polling run continuously; keys and accounts live in a SQLite file). Vercel and other serverless hosts can't run it.

- Start command: `python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}` (also in `railway.json` and `Procfile`). Run one instance only.
- Attach a persistent volume and point `DATABASE_PATH` at it, e.g. `/data/portal.db`.
- Set `APP_ACCESS_TOKEN` and `CREDENTIAL_MASTER_KEY` (back this one up: without it saved keys can't be decrypted). Optional: `ANTHROPIC_API_KEY`, `SERVER_EGRESS_IPS`, `NEWS_USER_AGENT`.

## Tests

```bash
pytest -q
```

The tests use recorded-shape responses and a fake exchange. No real keys or network calls are needed.

## Adding another exchange

Write a subclass of `ExchangeConnector` in `app/exchanges/`, add it to `app/exchanges/registry.py`, and add tests. The wizard and portal pick it up automatically.
