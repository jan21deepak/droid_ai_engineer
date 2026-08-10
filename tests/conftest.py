import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = "sqlite:///./data/test_tasks.db"
os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
os.environ.setdefault("GITHUB_TOKEN", "")
os.environ["CURSOR_API_KEY"] = ""
os.environ["TRIGGER_LABEL"] = "Cursor-complete"

import pytest
from fastapi.testclient import TestClient

import app.database as database
from app.config import get_settings
from app.models import Base


@pytest.fixture(autouse=True)
def clean_db(tmp_path):
    get_settings.cache_clear()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/tasks.db"
    os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
    os.environ["TRIGGER_LABEL"] = "Cursor-complete"
    os.environ["CURSOR_API_KEY"] = ""
    database.reset_for_tests()
    database.init_db()
    yield
    Base.metadata.drop_all(database.get_engine())
    database.reset_for_tests()
    get_settings.cache_clear()


@pytest.fixture
def client():
    from app.app import app

    with TestClient(app) as test_client:
        yield test_client


