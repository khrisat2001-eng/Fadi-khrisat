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

Not built yet: automatic strategy execution (signals are acted on with one click), and the news intelligence engine. See the plan for the phases.

## Run locally

```bash
pip install -e ".[dev]"
cp .env.example .env   # then fill in CREDENTIAL_MASTER_KEY and APP_ACCESS_TOKEN
set -a; source .env; set +a
uvicorn app.main:app_factory --factory --port 8000
```

Open http://localhost:8000 and sign in with `APP_ACCESS_TOKEN`. For local http, set `SECURE_COOKIES=false`. In production, run it behind HTTPS only.

Never paste exchange API keys into chat, email, code or URLs. The wizard form is the only place to enter them.

## Tests

```bash
pytest -q
```

The tests use recorded-shape responses and a fake exchange. No real keys or network calls are needed.

## Adding another exchange

Write a subclass of `ExchangeConnector` in `app/exchanges/`, add it to `app/exchanges/registry.py`, and add tests. The wizard and portal pick it up automatically.
