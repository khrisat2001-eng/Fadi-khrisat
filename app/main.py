"""FastAPI application: exchange connections portal API and static UI."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from app import logging_utils
from app.config import Settings
from app.connections.service import ConnectionService, ConnectorFactory, PortalError, default_connector_factory
from app.connections.store import Store
from app.exchanges.base import Credentials
from app.exchanges.registry import CONNECTORS
from app.marketdata.supervisor import Connect, MarketDataManager, websockets_connect
from app.paper.engine import PaperTradingService
from app.paper.store import TradingStore
from app.risk.config import RiskConfig
from app.risk.gate import DecisionGate, TradeRequest
from app.strategy.config import StrategyConfig
from app.strategy.service import CandleFetcher, CandleService, fetch_public_candles
from app.security.vault import CredentialVault, KeyProvider, LocalKeyProvider

log = logging.getLogger("app")
STATIC_DIR = Path(__file__).parent / "static"
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
) -> FastAPI:
    settings = settings or Settings()
    if not settings.access_token or len(settings.access_token) < 16:
        raise RuntimeError("APP_ACCESS_TOKEN must be set to a random value of at least 16 characters.")
    logging_utils.install()

    if settings.database_path != ":memory:":
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(settings.database_path)
    vault = CredentialVault(key_provider or LocalKeyProvider.from_env())
    service = ConnectionService(store, vault, connector_factory)
    market = MarketDataManager(market_connect, on_alert=lambda level, msg: store.add_alert(None, level, msg))
    paper = PaperTradingService(service, TradingStore(store), market, DecisionGate(), CandleService(candle_fetcher))
    session_value = hmac.new(settings.access_token.encode(), b"portal-session-v1", hashlib.sha256).hexdigest()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if start_market_data:
            await paper.sync_streams()
        if run_health_monitor:
            task = asyncio.create_task(_health_loop(service, paper, settings.health_interval_seconds))
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await market.stop_all()

    app = FastAPI(title="Exchange Connections Portal", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service
    app.state.paper = paper
    app.state.market = market

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


def app_factory() -> FastAPI:  # used by: uvicorn app.main:app_factory --factory
    return create_app()
