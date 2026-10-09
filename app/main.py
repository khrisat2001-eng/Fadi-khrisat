"""FastAPI application: exchange connections portal API and static UI."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
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


def create_app(
    settings: Settings | None = None,
    key_provider: KeyProvider | None = None,
    connector_factory: ConnectorFactory = default_connector_factory,
    run_health_monitor: bool = True,
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
    session_value = hmac.new(settings.access_token.encode(), b"portal-session-v1", hashlib.sha256).hexdigest()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if run_health_monitor:
            task = asyncio.create_task(_health_loop(service, settings.health_interval_seconds))
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Exchange Connections Portal", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service

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
        return service.update_settings(cid, body.allocation_pct, body.pairs)

    @app.post("/api/connections/{cid}/paper", dependencies=auth)
    async def enable_paper(cid: str):
        return service.enable_paper(cid)

    @app.post("/api/connections/{cid}/live", dependencies=auth)
    async def request_live(cid: str):
        service.request_live(cid)

    @app.put("/api/connections/{cid}/credentials", dependencies=auth)
    async def replace(cid: str, body: CredentialsIn):
        return await service.replace_credentials(cid, body.to_credentials())

    @app.delete("/api/connections/{cid}", dependencies=auth)
    async def disconnect(cid: str):
        return service.disconnect(cid)

    @app.get("/api/alerts", dependencies=auth)
    async def alerts():
        return service.store.list_alerts()

    @app.post("/api/alerts/{alert_id}/ack", dependencies=auth)
    async def ack(alert_id: int):
        service.store.acknowledge_alert(alert_id)
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


async def _health_loop(service: ConnectionService, interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await service.check_all()
        except Exception as exc:  # keep monitoring no matter what
            log.error("Health check loop error: %s", type(exc).__name__)


def app_factory() -> FastAPI:  # used by: uvicorn app.main:app_factory --factory
    return create_app()
