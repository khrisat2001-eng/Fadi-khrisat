"""FastAPI application: exchange connections portal API and static UI."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from app import logging_utils
from app.config import Settings, explain_setting
from app.connections.service import ConnectionService, ConnectorFactory, PortalError, default_connector_factory
from app.connections.store import Store
from app.exchanges.base import Credentials
from app.exchanges.registry import CONNECTORS
from app.marketdata.supervisor import Connect, MarketDataManager, websockets_connect
from app.news.classifier import Classifier
from app.news.config import NewsConfig
from app.news.service import NewsError, NewsService
from app.news.sources import Fetcher, http_fetch
from app.paper.autopilot import Autopilot
from app.paper.engine import PaperTradingService
from app.paper.home import home_view
from app.paper.store import TradingStore
from app.risk.config import RiskConfig
from app.risk.gate import DecisionGate, TradeRequest
from app.strategy.config import StrategyConfig
from app.strategy.service import CandleFetcher, CandleService, fetch_public_candles
from app.security.vault import CredentialVault, KeyProvider, LocalKeyProvider

log = logging.getLogger("app")
STATIC_DIR = Path(__file__).parent / "static"
AUTOPILOT_INTERVAL_SECONDS = 30
SESSION_COOKIE = "portal_session"


class CredentialsIn(BaseModel):
    # SecretStr keeps values out of reprs, validation errors and logs.
    api_key: SecretStr = Field(max_length=256)
    api_secret: SecretStr = Field(max_length=256)
    passphrase: SecretStr = Field(max_length=256)

    def to_credentials(self) -> Credentials:
        return Credentials(self.api_key.get_secret_value(), self.api_secret.get_secret_value(), self.passphrase.get_secret_value())


class ConnectIn(CredentialsIn):
    exchange: str


class SettingsIn(BaseModel):
    allocation_pct: float
    pairs: list[str] = Field(max_length=200)


class LoginIn(BaseModel):
    token: SecretStr


class PaperAccountIn(BaseModel):
    starting_balance: Decimal | None = Field(None, gt=0, le=Decimal("1e12"))


class PaperOrderIn(BaseModel):
    symbol: str = Field(max_length=40)
    source: str = Field("manual", pattern="^(manual|strategy)$")
    stop_price: Decimal | None = Field(None, gt=0)
    take_profit_price: Decimal | None = Field(None, gt=0)
    risk_pct: Decimal | None = Field(None, gt=0, le=5)


class StopIn(BaseModel):
    stop_price: Decimal = Field(gt=0)


class KillSwitchIn(BaseModel):
    on: bool
    reason: str = Field("", max_length=300)


class SourceToggleIn(BaseModel):
    enabled: bool


class CalendarIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scheduled_at: datetime
    impact: str = Field(pattern="^(low|medium|high)$")
    assets: list[str] = Field(default_factory=list, max_length=50)
    source_url: str = Field(max_length=500)
    time_verified: bool = False
    status: str = Field("scheduled", pattern="^(scheduled|confirmed|changed|completed|cancelled)$")
    notes: str = Field("", max_length=500)


class CalendarUpdateIn(BaseModel):
    status: str | None = Field(None, pattern="^(scheduled|confirmed|changed|completed|cancelled)$")
    time_verified: bool | None = None
    scheduled_at: datetime | None = None


class AutopilotIn(BaseModel):
    on: bool


class SuspensionIn(BaseModel):
    scope: str = Field(pattern="^(global|exchange|asset)$")
    target: str = Field(max_length=40)
    reason: str = Field(min_length=1, max_length=300)


def create_app(
    settings: Settings | None = None,
    key_provider: KeyProvider | None = None,
    connector_factory: ConnectorFactory = default_connector_factory,
    run_health_monitor: bool = True,
    market_connect: Connect = websockets_connect,
    start_market_data: bool = True,
    candle_fetcher: CandleFetcher = fetch_public_candles,
    news_fetcher: Fetcher = http_fetch,
    news_classifier: Classifier | None = None,
    run_news_monitor: bool | None = None,
    run_autopilot: bool | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if not settings.access_token or len(settings.access_token) < 16:
        raise RuntimeError(
            explain_setting("APP_ACCESS_TOKEN", settings.access_token,
                            f"only {len(settings.access_token)} characters long")
            + " It must be a random value of at least 16 characters."
        )
    logging_utils.install()

    if settings.database_path != ":memory:":
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(settings.database_path)
    vault = CredentialVault(key_provider or LocalKeyProvider.from_env())
    service = ConnectionService(store, vault, connector_factory)
    market = MarketDataManager(market_connect, on_alert=lambda level, msg: store.add_alert(None, level, msg))
    trading = TradingStore(store)
    news = NewsService(store, trading, market, news_classifier, news_fetcher)
    paper = PaperTradingService(service, trading, market, DecisionGate(), CandleService(candle_fetcher), news)
    autopilot = Autopilot(paper)
    if run_news_monitor is None:
        run_news_monitor = run_health_monitor
    if run_autopilot is None:
        run_autopilot = run_health_monitor
    session_value = hmac.new(settings.access_token.encode(), b"portal-session-v1", hashlib.sha256).hexdigest()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if start_market_data:
            await paper.sync_streams()
        if run_health_monitor:
            tasks.append(asyncio.create_task(_health_loop(service, paper, settings.health_interval_seconds)))
        if run_news_monitor:
            tasks.append(asyncio.create_task(_news_loop(news)))
        if run_autopilot:
            tasks.append(asyncio.create_task(_autopilot_loop(autopilot)))
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await market.stop_all()

    app = FastAPI(title="Exchange Connections Portal", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service
    app.state.paper = paper
    app.state.autopilot = autopilot
    app.state.market = market
    app.state.news = news

    async def resync_streams() -> None:
        if start_market_data:
            await paper.sync_streams()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PortalError)
    async def portal_error(_: Request, exc: PortalError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(NewsError)
    async def news_error(_: Request, exc: NewsError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(PermissionError)
    async def permission_error(_: Request, exc: PermissionError):
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        # Never echo submitted values back: they may be secrets.
        fields = sorted({str(e.get("loc", ["", "?"])[-1]) for e in exc.errors()})
        return JSONResponse({"error": f"Please check these fields: {', '.join(fields)}"}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception):
        log.error("Unhandled error: %s", type(exc).__name__)
        return JSONResponse({"error": "Something went wrong on our side. Please try again."}, status_code=500)

    def require_auth(request: Request) -> None:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        header = request.headers.get("authorization", "")
        bearer = header[7:] if header.lower().startswith("bearer ") else ""
        if cookie and hmac.compare_digest(cookie, session_value):
            return
        if bearer and hmac.compare_digest(bearer, settings.access_token):
            return
        raise HTTPException(status_code=401, detail="Please sign in.")

    auth = [Depends(require_auth)]

    @app.post("/api/login")
    async def login(body: LoginIn, response: Response):
        if not hmac.compare_digest(body.token.get_secret_value(), settings.access_token):
            raise HTTPException(status_code=401, detail="Wrong access token.")
        response.set_cookie(SESSION_COOKIE, session_value, httponly=True, samesite="strict", secure=settings.secure_cookies, max_age=12 * 3600)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/session", dependencies=auth)
    async def session():
        return {"ok": True}

    @app.get("/api/exchanges", dependencies=auth)
    async def exchanges():
        return [
            {**cls.guide().__dict__, "credential_fields": [f.__dict__ for f in cls.guide().credential_fields], "server_ips": settings.egress_ips}
            for cls in CONNECTORS.values()
        ]

    @app.get("/api/connections", dependencies=auth)
    async def list_connections():
        return service.list_views()

    @app.post("/api/connections", dependencies=auth)
    async def connect(body: ConnectIn):
        return await service.connect(body.exchange, body.to_credentials())

    @app.get("/api/connections/{cid}", dependencies=auth)
    async def get_connection(cid: str):
        return service.view(cid)

    @app.get("/api/connections/{cid}/audit", dependencies=auth)
    async def audit(cid: str):
        service.view(cid)
        return service.store.list_audit(cid)

    @app.post("/api/connections/{cid}/test", dependencies=auth)
    async def test(cid: str):
        return await service.test(cid)

    @app.post("/api/connections/{cid}/sync", dependencies=auth)
    async def sync(cid: str):
        return await service.sync_balances(cid)

    @app.put("/api/connections/{cid}/settings", dependencies=auth)
    async def update_settings(cid: str, body: SettingsIn):
        view = service.update_settings(cid, body.allocation_pct, body.pairs)
        await resync_streams()
        return view

    @app.post("/api/connections/{cid}/paper", dependencies=auth)
    async def enable_paper(cid: str):
        view = service.enable_paper(cid)
        await resync_streams()
        return view

    @app.post("/api/connections/{cid}/live", dependencies=auth)
    async def request_live(cid: str):
        service.request_live(cid)

    @app.put("/api/connections/{cid}/credentials", dependencies=auth)
    async def replace(cid: str, body: CredentialsIn):
        return await service.replace_credentials(cid, body.to_credentials())

    @app.delete("/api/connections/{cid}", dependencies=auth)
    async def disconnect(cid: str):
        view = service.disconnect(cid)
        paper.close_account(cid)
        await resync_streams()
        return view

    @app.get("/api/alerts", dependencies=auth)
    async def alerts():
        return service.store.list_alerts()

    @app.post("/api/alerts/{alert_id}/ack", dependencies=auth)
    async def ack(alert_id: int):
        service.store.acknowledge_alert(alert_id)
        return {"ok": True}

    # --- market data, paper trading and risk ---------------------------------------
    @app.get("/api/market", dependencies=auth)
    async def market_status():
        return {
            "streams": market.status(),
            "tickers": [
                {"exchange": t.exchange, "symbol": t.symbol, "bid": str(t.bid), "ask": str(t.ask), "last": str(t.last),
                 "spread_bps": f"{t.spread_bps:.1f}", "age_seconds": round(t.age_seconds(), 1)}
                for t in market.tickers()
            ],
        }

    @app.get("/api/paper/{cid}", dependencies=auth)
    async def paper_account(cid: str):
        return paper.account_view(cid)

    @app.post("/api/paper/{cid}/account", dependencies=auth)
    async def paper_create_account(cid: str, body: PaperAccountIn):
        return paper.create_account(cid, body.starting_balance)

    @app.post("/api/paper/{cid}/orders", dependencies=auth)
    async def paper_order(cid: str, body: PaperOrderIn):
        req = TradeRequest(cid, body.symbol, "buy", body.stop_price, body.take_profit_price, body.risk_pct, source=body.source)
        result = await paper.place_order(req)
        await resync_streams()
        return result

    @app.post("/api/paper/{cid}/positions/{symbol}/close", dependencies=auth)
    async def paper_close(cid: str, symbol: str):
        fill = paper.close_position(cid, symbol)
        await resync_streams()
        return fill

    @app.put("/api/paper/{cid}/positions/{symbol}/stop", dependencies=auth)
    async def paper_stop(cid: str, symbol: str, body: StopIn):
        return paper.update_stop(cid, symbol, body.stop_price)

    @app.get("/api/strategy/{cid}/signals", dependencies=auth)
    async def strategy_signals(cid: str):
        return await paper.signals(cid)

    @app.get("/api/strategy/config", dependencies=auth)
    async def get_strategy_config():
        return paper.strategy_config().model_dump()

    @app.put("/api/strategy/config", dependencies=auth)
    async def put_strategy_config(body: StrategyConfig):
        return paper.set_strategy_config(body).model_dump()

    @app.get("/api/decisions", dependencies=auth)
    async def decisions(connection_id: str | None = None):
        return paper.trading.decisions(connection_id)

    @app.get("/api/risk/config", dependencies=auth)
    async def get_risk_config():
        return paper.risk_config().as_json()

    @app.put("/api/risk/config", dependencies=auth)
    async def put_risk_config(body: RiskConfig):
        return paper.set_risk_config(body).as_json()

    @app.get("/api/autopilot/{cid}", dependencies=auth)
    async def get_autopilot(cid: str):
        return autopilot.status(cid)

    @app.put("/api/autopilot/{cid}", dependencies=auth)
    async def put_autopilot(cid: str, body: AutopilotIn):
        status = autopilot.set_on(cid, body.on)
        if body.on:
            await autopilot.run_once()  # first check right away, so the Home screen fills in
            status = autopilot.status(cid)
        return status

    @app.get("/api/home", dependencies=auth)
    async def home():
        return home_view(paper, autopilot)

    @app.get("/api/risk/kill-switch", dependencies=auth)
    async def get_kill_switch():
        return {"on": paper.kill_switch()}

    @app.post("/api/risk/kill-switch", dependencies=auth)
    async def set_kill_switch(body: KillSwitchIn):
        paper.set_kill_switch(body.on, body.reason)
        return {"on": paper.kill_switch()}

    @app.get("/api/risk/suspensions", dependencies=auth)
    async def list_suspensions():
        return paper.trading.active_suspensions()

    @app.post("/api/risk/suspensions", dependencies=auth)
    async def add_suspension(body: SuspensionIn):
        target = "*" if body.scope == "global" else body.target.strip().lower() if body.scope == "exchange" else body.target.strip().upper()
        paper.trading.add_suspension(body.scope, target, body.reason)
        store.audit(None, "suspension_added", f"{body.scope} {target}: {body.reason}")
        return paper.trading.active_suspensions()

    @app.delete("/api/risk/suspensions/{sid}", dependencies=auth)
    async def lift_suspension(sid: int):
        paper.trading.lift_suspension(sid)
        store.audit(None, "suspension_lifted", str(sid))
        return paper.trading.active_suspensions()

    # --- news intelligence ------------------------------------------------------------
    @app.get("/api/news", dependencies=auth)
    async def news_feed(asset: str | None = None, exchange: str | None = None, category: str | None = None,
                        sentiment: str | None = None, confirmation: str | None = None, severity: str | None = None,
                        source: str | None = None, since: str | None = None, limit: int = 100):
        return news.feed(asset, exchange, category, sentiment, confirmation, severity, source, since, max(1, min(limit, 300)))

    @app.get("/api/news/overview", dependencies=auth)
    async def news_overview():
        assets = []
        for c in store.list_connections():
            if c["state"] != "PAPER":
                continue
            signals = {s["symbol"]: s for s in await paper.signals(c["id"])}
            for symbol in c["selected_pairs"]:
                ctx = await paper.build_context(c, symbol)
                sig = signals.get(symbol) or {}
                assets.append({"connection_id": c["id"], "exchange": c["exchange"], "symbol": symbol,
                               "technical": {"status": sig.get("status"), "reasons": sig.get("reasons")},
                               "news": ctx.news.as_dict() if ctx.news else None})
        return {**news.overview(), "assets": assets}

    @app.post("/api/news/poll", dependencies=auth)
    async def news_poll():
        return await news.poll_once()

    @app.put("/api/news/sources/{source_id}", dependencies=auth)
    async def news_source(source_id: str, body: SourceToggleIn):
        return news.set_source_enabled(source_id, body.enabled)

    @app.get("/api/news/config", dependencies=auth)
    async def get_news_config():
        return news.config().model_dump()

    @app.put("/api/news/config", dependencies=auth)
    async def put_news_config(body: NewsConfig):
        return news.set_config(body).model_dump()

    @app.get("/api/news/calendar", dependencies=auth)
    async def news_calendar():
        return news.calendar()

    @app.post("/api/news/calendar", dependencies=auth)
    async def add_calendar(body: CalendarIn):
        return news.add_calendar(body.name, body.scheduled_at, body.impact, body.assets, body.source_url,
                                 body.time_verified, body.status, body.notes)

    @app.put("/api/news/calendar/{cal_id}", dependencies=auth)
    async def update_calendar(cal_id: int, body: CalendarUpdateIn):
        return news.update_calendar(cal_id, body.status, body.time_verified, body.scheduled_at)

    @app.delete("/api/news/calendar/{cal_id}", dependencies=auth)
    async def delete_calendar(cal_id: int):
        return news.delete_calendar(cal_id)

    @app.get("/api/news/performance", dependencies=auth)
    async def news_performance():
        return news.performance()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


async def _health_loop(service: ConnectionService, paper: PaperTradingService, interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await service.check_all()
            await paper.sync_streams()
        except Exception as exc:  # keep monitoring no matter what
            log.error("Health check loop error: %s", type(exc).__name__)


async def _autopilot_loop(autopilot: Autopilot) -> None:
    while True:
        await asyncio.sleep(AUTOPILOT_INTERVAL_SECONDS)
        try:
            await autopilot.run_once()
        except Exception as exc:  # keep running no matter what
            log.error("Autopilot loop error: %s", type(exc).__name__)


async def _news_loop(news: NewsService) -> None:
    while True:
        try:
            await news.poll_once()
        except Exception as exc:  # keep monitoring no matter what
            log.error("News loop error: %s", type(exc).__name__)
        await asyncio.sleep(news.config().poll_interval_seconds)


def app_factory() -> FastAPI:  # used by: uvicorn app.main:app_factory --factory
    return create_app()


def _setup_needed_app(problem: str) -> FastAPI:
    """Served when required settings are missing, so the host starts and says what to fix."""
    setup = FastAPI(title="Setup needed", docs_url=None, redoc_url=None, openapi_url=None)

    @setup.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def setup_needed(path: str):
        return JSONResponse({"error": f"Setup needed: {problem}"}, status_code=503)

    return setup


# Top-level instance for hosts and tools that look for `app` (e.g. `fastapi run app/main.py`).
# Without APP_ACCESS_TOKEN and CREDENTIAL_MASTER_KEY it serves only a "setup needed" message.
VERCEL_MESSAGE = (
    "This app can't run on Vercel. It needs an always-on server with a persistent disk: live price streams, "
    "stop-loss checks and news polling run continuously, and the encrypted key store is a database file. "
    "Vercel functions are short-lived with a read-only, temporary disk. Deploy it to Render, Railway, Fly.io or a VPS."
)

if os.environ.get("VERCEL"):
    app = _setup_needed_app(VERCEL_MESSAGE)
else:
    try:
        app = create_app()
    except (RuntimeError, ValueError, OSError) as exc:
        log.warning("App not started: %s", exc)
        app = _setup_needed_app(str(exc))
