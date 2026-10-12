"""Additive, idempotent upgrades of a temporary pre-attempt database.

The old schema below is the one shipped before per-turn attempts, delivery
controls and candidate tracking existed. Only throwaway databases under
``tmp_path`` are created and upgraded; ``data/tasks.db`` is never opened.
"""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import database, reconcile, recovery, worker
from app.config import get_settings
from app.models import ACCOUNTING_LEGACY_SUMMARY, TaskStatus
from app.run_models import UsageCompleteness
from tests.conftest import PROTECTED_BASE_URL
from tests.test_run_attempts import BEARER, REPO, SHA_A, FakeDroid, FakeGitHub, turn

OLD_SCHEMA = """
CREATE TABLE repositories (
    id INTEGER NOT NULL, full_name VARCHAR(255) NOT NULL, url VARCHAR(512) NOT NULL,
    description TEXT NOT NULL, setup_command VARCHAR(1000) NOT NULL, droid_model VARCHAR(128) NOT NULL,
    starting_ref VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id)
);
CREATE TABLE review_tasks (
    id INTEGER NOT NULL, repository VARCHAR(255) NOT NULL, repository_url VARCHAR(512) NOT NULL,
    pr_number INTEGER NOT NULL, pr_title VARCHAR(512) NOT NULL, pr_url VARCHAR(512) NOT NULL,
    commit_sha VARCHAR(64), droid_session_id VARCHAR(128), status VARCHAR(32) NOT NULL,
    merged BOOLEAN NOT NULL, auto_merge_enabled BOOLEAN NOT NULL, summary TEXT, error TEXT,
    factory_credits FLOAT, cost_usd FLOAT, created_at DATETIME NOT NULL, completed_at DATETIME,
    duration_seconds FLOAT, PRIMARY KEY (id)
);
CREATE TABLE synced_issues (
    id INTEGER NOT NULL, repository VARCHAR(255) NOT NULL, repository_url VARCHAR(512) NOT NULL,
    issue_number INTEGER NOT NULL, title VARCHAR(512) NOT NULL, body TEXT NOT NULL, state VARCHAR(32) NOT NULL,
    labels TEXT NOT NULL, html_url VARCHAR(512) NOT NULL, synced_at DATETIME NOT NULL, PRIMARY KEY (id),
    CONSTRAINT uq_repo_issue UNIQUE (repository, issue_number)
);
CREATE TABLE synced_pull_requests (
    id INTEGER NOT NULL, repository VARCHAR(255) NOT NULL, repository_url VARCHAR(512) NOT NULL,
    pr_number INTEGER NOT NULL, title VARCHAR(512) NOT NULL, body TEXT NOT NULL, html_url VARCHAR(512) NOT NULL,
    head_sha VARCHAR(64) NOT NULL, author VARCHAR(128) NOT NULL, draft BOOLEAN NOT NULL,
    synced_at DATETIME NOT NULL, PRIMARY KEY (id), CONSTRAINT uq_repo_pr UNIQUE (repository, pr_number)
);
CREATE TABLE tasks (
    id INTEGER NOT NULL, repository VARCHAR(255) NOT NULL, repository_url VARCHAR(512) NOT NULL,
    issue_number INTEGER NOT NULL, issue_title VARCHAR(512) NOT NULL, issue_body TEXT NOT NULL,
    labels TEXT NOT NULL, droid_session_id VARCHAR(128), status VARCHAR(32) NOT NULL,
    pull_request_url VARCHAR(512), pr_state VARCHAR(16), pr_merged_at DATETIME, summary TEXT, error TEXT,
    factory_credits FLOAT, estimated_tokens INTEGER, cost_usd FLOAT, created_at DATETIME NOT NULL,
    completed_at DATETIME, duration_seconds FLOAT, PRIMARY KEY (id)
);
"""

URL = f"https://github.com/{REPO}"
CREATED = "2025-01-02 03:04:05.000000"
DONE = "2025-01-02 03:14:05.000000"
COMPLETED_TASK, FAILED_TASK, ACTIVE_TASK, COMPLETED_REVIEW = 11, 12, 13, 21
OLD_TASK_COLUMNS = (
    "id, repository, repository_url, issue_number, issue_title, issue_body, labels, droid_session_id, status, "
    "pull_request_url, pr_state, pr_merged_at, summary, error, factory_credits, estimated_tokens, cost_usd, "
    "created_at, completed_at, duration_seconds"
)
OLD_REVIEW_COLUMNS = (
    "id, repository, repository_url, pr_number, pr_title, pr_url, commit_sha, droid_session_id, status, merged, "
    "auto_merge_enabled, summary, error, factory_credits, cost_usd, created_at, completed_at, duration_seconds"
)


def build_old_database(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    tasks = [
        (COMPLETED_TASK, REPO, URL, 1, "Fix docs", "", "[]", "sess-old-done", "completed", None, None, None,
         "Legacy summary of the fix", None, 40.0, 900, 0.25, CREATED, DONE, 600.0),
        (FAILED_TASK, REPO, URL, 2, "Broken build", "", "[]", None, "failed", None, None, None,
         None, "Droid run ended with status: error", None, None, None, CREATED, DONE, None),
        (ACTIVE_TASK, REPO, URL, 3, "Still running", "", "[]", "sess-old-live", "running", None, None, None,
         None, None, None, None, None, CREATED, None, None),
    ]  # fmt: skip
    conn.executemany(f"INSERT INTO tasks ({OLD_TASK_COLUMNS}) VALUES ({', '.join('?' * 20)})", tasks)
    conn.execute(
        f"INSERT INTO review_tasks ({OLD_REVIEW_COLUMNS}) VALUES ({', '.join('?' * 18)})",
        (COMPLETED_REVIEW, REPO, URL, 7, "Old PR", f"{URL}/pull/7", SHA_A, "sess-old-review", "completed",
         0, 0, "Looks fine", None, 3.0, None, CREATED, DONE, 120.0),
    )  # fmt: skip
    conn.commit()
    conn.close()


def snapshot(path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {
            "tasks": conn.execute(f"SELECT {OLD_TASK_COLUMNS} FROM tasks ORDER BY id").fetchall(),
            "reviews": conn.execute(f"SELECT {OLD_REVIEW_COLUMNS} FROM review_tasks ORDER BY id").fetchall(),
            "attempts": conn.execute("SELECT count(*) FROM run_attempts").fetchone()
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'run_attempts'").fetchone()
            else None,
            "tracked": conn.execute(
                "SELECT attempts_tracked FROM tasks UNION ALL SELECT attempts_tracked FROM review_tasks"
            ).fetchall()
            if "attempts_tracked" in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            else None,
        }
    finally:
        conn.close()


def schema(path) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    path = tmp_path / "old-schema.db"
    build_old_database(path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", REPO)
    get_settings.cache_clear()
    database.reset_for_tests()
    return path


def upgrade() -> None:
    database.reset_for_tests()
    database.init_db()


def test_two_upgrades_preserve_old_rows_and_invent_nothing(old_db):
    before = snapshot(old_db)
    assert before["attempts"] is None and before["tracked"] is None

    upgrade()
    first_schema, first = schema(old_db), snapshot(old_db)
    upgrade()

    assert schema(old_db) == first_schema
    assert snapshot(old_db) == first
    assert first["tasks"] == before["tasks"]
    assert first["reviews"] == before["reviews"]
    # No historical attempts are fabricated, and old rows are marked as untracked history.
    assert first["attempts"] == (0,)
    assert first["tracked"] == [(None,)] * 4
    names = {name for _, name, _ in first_schema}
    assert {"run_attempts", "run_events", "uq_attempt_one_active", "webhook_deliveries"} <= names


LEDGER_COLUMNS = (
    "usage_snapshot",
    "usage_snapshot_at",
    "estimated_usd",
    "estimate_rate_usd_per_credit",
    "estimate_source",
    "estimated_at",
    "reconciled_usd",
    "reconciliation_source",
    "reconciled_at",
)


def test_attempts_table_from_before_the_usage_ledger_is_upgraded_in_place(old_db):
    upgrade()
    conn = sqlite3.connect(old_db)
    conn.execute("DROP TABLE run_events")
    for column in LEDGER_COLUMNS:
        conn.execute(f"ALTER TABLE run_attempts DROP COLUMN {column}")
    conn.execute(
        "INSERT INTO run_attempts (id, parent_type, parent_id, kind, sequence, status, created_at, "
        "factory_credits, usage_source, usage_completeness) "
        "VALUES (5, 'task', ?, 'follow_up', 1, 'completed', ?, 5.0, 'terminal_result', 'complete')",
        (COMPLETED_TASK, CREATED),
    )
    conn.commit()
    before = conn.execute("SELECT id, parent_id, kind, status, factory_credits FROM run_attempts").fetchall()
    conn.close()

    upgrade()
    upgrade()

    conn = sqlite3.connect(old_db)
    try:
        assert (
            conn.execute("SELECT id, parent_id, kind, status, factory_credits FROM run_attempts").fetchall() == before
        )
        added = conn.execute(f"SELECT {', '.join(LEDGER_COLUMNS)} FROM run_attempts").fetchall()
        # Nothing is back-filled: no invented snapshot, estimate or reconciliation.
        assert added == [(None,) * len(LEDGER_COLUMNS)]
        assert conn.execute("SELECT count(*) FROM run_events").fetchone() == (0,)
    finally:
        conn.close()


def test_legacy_rows_stay_readable_and_new_follow_up_is_counted_once(old_db, protected_mode, monkeypatch):
    upgrade()
    upgrade()
    droid = FakeDroid()
    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
    monkeypatch.setattr(recovery, "get_droid_client", lambda: droid)
    monkeypatch.setattr(worker, "GitHubClient", lambda: FakeGitHub())
    monkeypatch.setattr(reconcile, "GitHubClient", lambda: FakeGitHub())
    from app.app import app

    with TestClient(app, base_url=PROTECTED_BASE_URL) as api:
        api.headers.update(BEARER)
        done = api.get(f"/api/tasks/{COMPLETED_TASK}").json()
        assert done["task"]["status"] == TaskStatus.COMPLETED
        assert (done["task"]["droid_session_id"], done["task"]["summary"]) == (
            "sess-old-done",
            "Legacy summary of the fix",
        )
        assert done["attempts"] == []
        assert done["accounting"]["accounting_basis"] == ACCOUNTING_LEGACY_SUMMARY
        assert done["accounting"]["historical_summary"]["factory_credits"] == 40.0
        assert done["accounting"]["coverage"] == UsageCompleteness.PARTIAL

        failed = api.get(f"/api/tasks/{FAILED_TASK}").json()
        assert failed["attempts"] == []
        assert failed["task"]["factory_credits"] is None and failed["task"]["estimated_tokens"] is None
        assert failed["accounting"]["observed_factory_credits"] is None
        assert failed["accounting"]["coverage"] == UsageCompleteness.UNKNOWN

        review = api.get(f"/api/reviews/{COMPLETED_REVIEW}").json()
        assert (review["review"]["commit_sha"], review["review"]["status"]) == (SHA_A, TaskStatus.COMPLETED)
        assert review["attempts"] == []
        assert review["accounting"]["historical_summary"]["factory_credits"] == 3.0

        # Startup recovery classified the orphaned legacy task without resuming or erasing it.
        active = api.get(f"/api/tasks/{ACTIVE_TASK}").json()
        assert (active["task"]["status"], active["task"]["droid_session_id"]) == (
            TaskStatus.INTERRUPTED,
            "sess-old-live",
        )
        assert active["attempts"] == []
        assert droid.turns == []

        listed = {t["id"] for t in api.get("/api/tasks").json()["tasks"]}
        assert {COMPLETED_TASK, FAILED_TASK, ACTIVE_TASK} <= listed

        resp = api.post(f"/api/tasks/{COMPLETED_TASK}/follow-up", json={"instruction": "tidy up"})
        assert resp.status_code == 200, resp.text
        asyncio.run(droid.turns[0]["on_complete"](turn(5, session_id="sess-old-done")))
        asyncio.run(droid.turns[0]["on_complete"](turn(5, session_id="sess-old-done")))

        after = api.get(f"/api/tasks/{COMPLETED_TASK}").json()

    [attempt] = after["attempts"]
    assert (attempt["kind"], attempt["sequence"], attempt["status"]) == ("follow_up", 1, "completed")
    assert attempt["usage"]["factory_credits"] == 5
    accounting = after["accounting"]
    assert accounting["attempt_factory_credits"] == 5
    assert accounting["historical_summary"]["factory_credits"] == 40.0
    assert accounting["observed_factory_credits"] == 45
    assert accounting["coverage"] == UsageCompleteness.PARTIAL
    assert accounting["usage_totals"]["factory_credits"] == 5
    # The legacy dollar figure stays history; the new attempt's dollars are unknown, not zero.
    assert accounting["historical_summary"]["cost_usd"] == 0.25
    assert accounting["usd"]["coverage"] == UsageCompleteness.UNKNOWN
    assert accounting["usd"]["known_usd_subtotal"] is None
    assert attempt["cost"]["status"] == "unknown"
    # The historical summary stays as recorded; the new turn is not folded into it.
    assert after["task"]["factory_credits"] == 40.0
    assert after["task"]["accounting_basis"] == ACCOUNTING_LEGACY_SUMMARY
    assert after["task"]["cost_usd"] == 0.25
