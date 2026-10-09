import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security.vault import LocalKeyProvider

from .fakes import FakeExchange

TOKEN = "test-access-token-0123456789"


@pytest.fixture
def fx():
    return FakeExchange()


@pytest.fixture
def client(fx, tmp_path):
    settings = Settings(access_token=TOKEN, database_path=str(tmp_path / "t.db"), egress_ips=["203.0.113.10"], secure_cookies=False)
    app = create_app(settings, LocalKeyProvider(os.urandom(32)), fx.factory, run_health_monitor=False)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        c.app_ref = app
        yield c
