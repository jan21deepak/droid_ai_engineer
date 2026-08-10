"""Tests for Cursor environment discovery used by the dashboard dropdown."""

from app.config import get_settings
from app.database import db_session
from app.models import Repository


def test_list_cursor_environments_includes_saved_repo_envs(client, monkeypatch):
    with db_session() as session:
        session.add(
            Repository(
                full_name="jan21deepak/omnigent",
                url="https://github.com/jan21deepak/omnigent",
                cursor_environment="omnigent",
            )
        )

    async def fake_list(self, *, limit: int = 50):
        return [
            {
                "name": "superset",
                "repositories": ["https://github.com/jan21deepak/superset"],
                "source": "cursor",
            }
        ]

    monkeypatch.setenv("CURSOR_API_KEY", "crsr_test")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.cursor_client.CursorClient.list_environments",
        fake_list,
    )

    resp = client.get("/api/cursor/environments")
    assert resp.status_code == 200
    data = resp.json()
    names = [item["name"] for item in data["environments"]]
    assert names == ["omnigent", "superset"]
