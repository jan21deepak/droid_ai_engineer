"""Selected-task evidence export: authenticated, bounded, redacted and free of unrelated history.

Uses the labeled local Git fixtures in ``tests/delivery_fixtures.py``; no GitHub or SDK call is made.
"""

import json

import httpx
import pytest

from app.config import get_settings
from app.database import db_session
from app.run_events import EventType, append_event
from app.run_models import AttemptKind, AttemptStatus, RunAttempt
from tests.conftest import OPERATOR_TOKEN
from tests.delivery_fixtures import configure
from tests.test_economics import accept, add_attempt
from tests.test_operator_ui import add_task, publish_fixture

FAKE_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the export must not make network calls")

    # GitHub is reached only through httpx.AsyncClient; the sync client is the test transport itself.
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)


def seeded(client, monkeypatch, tmp_path) -> tuple[int, str, int]:
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
    remote = configure(monkeypatch, tmp_path)
    get_settings.cache_clear()
    task_id, sha = accept(client, remote)
    approval = client.get(f"/api/tasks/{task_id}/delivery").json()["approval"]["current"]
    publish_fixture(task_id, sha, approval["id"])
    attempt_id = add_attempt(task_id, 1, status=AttemptStatus.FAILED, credits=3)
    add_attempt(task_id, 2, kind=AttemptKind.FOLLOW_UP, credits=4, reconciled=0.5)
    with db_session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        assert attempt is not None
        attempt.summary = "AGENT-SUMMARY-TEXT should stay out"
        attempt.error = f"push failed with token {FAKE_TOKEN}"
        append_event(session, attempt_id, EventType.ASSISTANT_MESSAGE, {"text": "ASSISTANT-EXCERPT"})
    other = add_task(issue_title="UNRELATED-OTHER-TASK", issue_number=99)
    add_attempt(other, 1, credits=50)
    return task_id, sha, other


class TestEvidenceExport:
    def test_export_holds_selected_task_facts_only(self, client, monkeypatch, tmp_path, no_network):
        task_id, sha, other = seeded(client, monkeypatch, tmp_path)
        assert (
            client.put(
                f"/api/tasks/{task_id}/human-effort", json={"review_minutes": 20, "rework_minutes": 5}
            ).status_code
            == 200
        )
        body = {"baseline_minutes_per_change": 120, "source": "fixture estimate"}
        assert client.put("/api/economics/assumptions", json=body).status_code == 200

        response = client.get(f"/api/tasks/{task_id}/evidence")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        doc = response.json()
        assert doc["format"] == "droid-forge.task-evidence.v1"
        assert doc["task"]["id"] == task_id and doc["task"]["accepted"] is True
        assert doc["candidate"]["sha"] == sha
        assert doc["approved_sha"] == sha
        assert [a["sequence"] for a in doc["attempts"]] == [1, 2]
        assert doc["attempts"][0]["status"] == "failed"
        assert doc["attempts"][0]["timeline_event_counts"] == {"assistant_message": 1}
        assert doc["checks"]["status"] == "passed"
        assert doc["checks"]["latest_run"]["results"][0] == {
            "name": "unit",
            "required": True,
            "status": "passed",
            "exit_code": None,
            "duration_seconds": None,
        }
        assert doc["review"]["latest"]["reviewed_sha"] == sha
        assert doc["review"]["latest"]["link"].startswith("/reviews/")
        publication = doc["publication"]
        assert publication["state"] == "published"
        assert publication["publication"]["pull_request"]["number"] == 101
        assert publication["remote_ci"]["state"] == "passing"
        assert publication["remote_ci"]["observed_age_seconds"] is not None
        assert doc["usage"]["observed_factory_credits"] == 7
        assert doc["usage"]["unsuccessful_attempt_factory_credits"] == 3
        assert doc["usage"]["usd"]["total_usd"] is None
        assert doc["human_effort"]["source"] == "user_entered"
        assert doc["assumptions"]["source"] == "fixture estimate"
        assert doc["merge"]["enabled"] is False

        text = response.text
        for absent in ("UNRELATED-OTHER-TASK", "AGENT-SUMMARY-TEXT", "ASSISTANT-EXCERPT", FAKE_TOKEN, "thinking_text"):
            assert absent not in text, absent
        assert "output_tail" not in text and '"command"' not in text
        assert f'"id": {other}' not in json.dumps(doc["task"])

    def test_download_is_an_attachment_with_a_fixed_name(self, client):
        task_id = add_task()
        response = client.get(f"/api/tasks/{task_id}/evidence", params={"download": "true"})
        assert response.status_code == 200
        assert response.headers["content-disposition"] == f'attachment; filename="forge-task-{task_id}-evidence.json"'
        doc = response.json()
        assert doc["approved_sha"] is None
        assert doc["publication"]["state"] == "not_published"
        assert doc["usage"]["observed_factory_credits"] is None
        assert doc["human_effort"]["recorded"] is False

    @pytest.mark.parametrize(
        "path",
        [
            "/api/tasks/9999/evidence",
            "/api/tasks/abc/evidence",
            "/api/tasks/..%2F..%2Fetc%2Fpasswd/evidence",
            "/api/tasks/1;cat/evidence",
            "/api/tasks/%2Fetc%2Fpasswd/evidence",
        ],
    )
    def test_unknown_and_path_shaped_selectors_are_refused(self, client, path):
        response = client.get(path)
        assert response.status_code in (404, 422)
        assert "root:" not in response.text

    def test_extra_parameters_never_read_files(self, client):
        task_id = add_task()
        response = client.get(f"/api/tasks/{task_id}/evidence", params={"path": "/etc/passwd", "file": "../.env"})
        assert response.status_code == 200
        assert "root:" not in response.text and "FORGE_OPERATOR_TOKEN" not in response.text

    def test_export_requires_operator_access(self, protected_client):
        task_id = add_task()
        assert protected_client.get(f"/api/tasks/{task_id}/evidence").status_code == 401
        page = protected_client.get(
            f"/api/tasks/{task_id}/evidence?download=true", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert page.status_code in (303, 401)
        bearer = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
        assert protected_client.get(f"/api/tasks/{task_id}/evidence", headers=bearer).status_code == 200
