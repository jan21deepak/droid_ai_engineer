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
from app.config import get_settings
from app.droid_client import reset_droid_client_for_tests
from app.models import Base


@pytest.fixture(autouse=True)
def clean_db(tmp_path):
    get_settings.cache_clear()
    reset_droid_client_for_tests()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/tasks.db"
    os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
    os.environ["TRIGGER_LABEL"] = "Droid-complete"
    os.environ["FACTORY_API_KEY"] = ""
    os.environ["DROID_ALLOW_CLI_AUTH"] = "false"
    database.reset_for_tests()
    database.init_db()
    yield
    Base.metadata.drop_all(database.get_engine())
    database.reset_for_tests()
    get_settings.cache_clear()
    reset_droid_client_for_tests()


@pytest.fixture
def client():
    from app.app import app

    with TestClient(app) as test_client:
        yield test_client
