"""Tests for the Droid model catalog endpoint and per-repo launch config."""

from app.database import db_session
from app.droid_client import DroidClient
from app.models import Repository
from app.repos import resolve_agent_launch_config


class StubDroid:
    configured = True

    def __init__(self, models):
        self._models = models

    async def list_available_models(self):
        return self._models


def test_list_droid_models(client, monkeypatch):
    import app.app as app_module

    stub = StubDroid(
        [
            {"id": "auto", "display_name": "Auto Model", "provider": "FACTORY"},
            {"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5", "provider": "ANTHROPIC"},
        ]
    )
    monkeypatch.setattr(app_module, "get_droid_client", lambda: stub)

    resp = client.get("/api/droid/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert data["models"][0]["id"] == "auto"


def test_list_droid_models_unconfigured(client, monkeypatch):
    import app.app as app_module

    monkeypatch.setattr(app_module, "get_droid_client", lambda: DroidClient(api_key=""))
    resp = client.get("/api/droid/models")
    assert resp.status_code == 200
    assert resp.json() == {"models": [], "count": 0}


def test_repository_patch_updates_launch_config(client):
    with db_session() as session:
        repo = Repository(
            full_name="jan21deepak/omnigent",
            url="https://github.com/jan21deepak/omnigent",
        )
        session.add(repo)
        session.flush()
        repo_id = repo.id

    resp = client.patch(
        f"/api/repositories/{repo_id}",
        json={"droid_model": "claude-sonnet-4-5", "setup_command": "npm ci", "starting_ref": "develop"},
    )
    assert resp.status_code == 200
    body = resp.json()["repository"]
    assert body["droid_model"] == "claude-sonnet-4-5"
    assert body["setup_command"] == "npm ci"
    assert body["starting_ref"] == "develop"

    launch = resolve_agent_launch_config(repository="jan21deepak/omnigent")
    assert launch == {
        "model": "claude-sonnet-4-5",
        "setup_command": "npm ci",
        "starting_ref": "develop",
    }


def test_launch_config_defaults_for_unregistered_repo():
    launch = resolve_agent_launch_config(repository="somebody/unknown")
    assert launch == {"model": None, "setup_command": None, "starting_ref": None}
