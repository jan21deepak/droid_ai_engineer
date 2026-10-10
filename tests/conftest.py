import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = "sqlite:///./data/test_tasks.db"
os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
os.environ.setdefault("GITHUB_TOKEN", "")
os.environ["FACTORY_API_KEY"] = ""
os.environ["DROID_ALLOW_CLI_AUTH"] = "false"
os.environ["TRIGGER_LABEL"] = "Droid-complete"
os.environ["WORKSPACE_ROOT"] = "./data/test-workspace"

import pytest
from fastapi.testclient import TestClient

import app.database as database
from app.auth import reset_sessions_for_tests
from app.config import get_settings
from app.droid_client import reset_droid_client_for_tests
from app.intake import reset_deliveries_for_tests
from app.models import Base

# Fake operator credential used only by protected-mode tests.
OPERATOR_TOKEN = "test-operator-token-0123456789-abcdefghijklmnop"
PROTECTED_BASE_URL = "http://127.0.0.1:8010"
PROTECTED_ORIGIN = PROTECTED_BASE_URL


def _reset_access_env() -> None:
    # Legacy route tests predate operator authentication; they run in the
    # explicit development mode rather than relying on an anonymous fallback.
    os.environ["FORGE_ACCESS_MODE"] = "local_dev"
    os.environ["FORGE_OPERATOR_TOKEN"] = ""
    os.environ["FORGE_TRUSTED_HOSTS"] = "testserver,test,127.0.0.1,localhost"
    os.environ["FORGE_ALLOWED_REPOSITORIES"] = ""
    os.environ["FORGE_COOKIE_SECURE"] = "auto"
    os.environ.pop("FORGE_SESSION_TTL_SECONDS", None)
    os.environ["PUBLIC_BASE_URL"] = ""


@pytest.fixture(autouse=True)
def clean_db(tmp_path):
    get_settings.cache_clear()
    reset_droid_client_for_tests()
    reset_sessions_for_tests()
    reset_deliveries_for_tests()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/tasks.db"
    os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
    os.environ["TRIGGER_LABEL"] = "Droid-complete"
    os.environ["FACTORY_API_KEY"] = ""
    os.environ["DROID_ALLOW_CLI_AUTH"] = "false"
    _reset_access_env()
    database.reset_for_tests()
    database.init_db()
    yield
    Base.metadata.drop_all(database.get_engine())
    database.reset_for_tests()
    _reset_access_env()
    reset_sessions_for_tests()
    get_settings.cache_clear()
    reset_droid_client_for_tests()


@pytest.fixture
def client():
    """Client for legacy route tests, running in explicit local_dev mode."""
    from app.app import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def protected_mode(monkeypatch):
    """Switch the app into protected mode with a configured operator credential."""
    monkeypatch.setenv("FORGE_ACCESS_MODE", "protected")
    monkeypatch.setenv("FORGE_OPERATOR_TOKEN", OPERATOR_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def protected_client(protected_mode):
    """Loopback client against the protected app (no session yet)."""
    from app.app import app

    with TestClient(app, base_url=PROTECTED_BASE_URL) as test_client:
        yield test_client
