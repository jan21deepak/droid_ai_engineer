"""Tests for worker runtime helpers (formatting, parsing, completion comments)."""

from datetime import datetime, timezone

from app.database import db_session
from app.models import Task, TaskStatus
from app.worker import _format_runtime, _parse_pr_number, build_completion_comment


def test_format_runtime():
    assert _format_runtime(None) == "unknown"
    assert _format_runtime(45) == "45s"
    assert _format_runtime(734) == "12m 14s"
    assert _format_runtime(7340) == "2h 2m 20s"


def test_parse_pr_number():
    assert _parse_pr_number("https://github.com/org/repo/pull/42") == 42
    assert _parse_pr_number("https://github.com/org/repo/pull/42/") == 42
    assert _parse_pr_number("https://github.com/org/repo") is None
    assert _parse_pr_number("not a url") is None


def test_build_completion_comment():
    task = Task(
        repository="jan21deepak/omnigent",
        issue_number=2,
        issue_title="[Bug] cold start",
        droid_session_id="sess-abc",
        status=TaskStatus.COMPLETED,
        pull_request_url="https://github.com/jan21deepak/omnigent/pull/7",
        summary="Fixed the guard clause and added tests.",
        duration_seconds=734.0,
        completed_at=datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc),
    )
    comment = build_completion_comment(task)
    assert "sess-abc" in comment
    assert "pull/7" in comment
    assert "12m 14s" in comment
    assert "Fixed the guard clause" in comment
    assert "✅" in comment


def test_task_to_dict_uses_droid_session_fields():
    with db_session() as session:
        task = Task(
            repository="jan21deepak/omnigent",
            issue_number=2,
            issue_title="[Bug] cold start",
            droid_session_id="sess-xyz",
            status=TaskStatus.COMPLETED,
        )
        session.add(task)
        session.flush()
        payload = task.to_dict()
    assert payload["droid_session_id"] == "sess-xyz"
    assert payload["status"] == "completed"
    assert payload["kind"] == "fix"
