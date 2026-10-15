"""Tests for worker runtime helpers (formatting, parsing, completion comments)."""

from datetime import UTC, datetime

from app.candidates import build_candidate_comment, format_runtime
from app.database import db_session
from app.models import Task, TaskStatus
from app.worker import parse_pr_number


def testformat_runtime():
    assert format_runtime(None) == "unknown"
    assert format_runtime(45) == "45s"
    assert format_runtime(734) == "12m 14s"
    assert format_runtime(7340) == "2h 2m 20s"


def testparse_pr_number():
    assert parse_pr_number("https://github.com/org/repo/pull/42") == 42
    assert parse_pr_number("https://github.com/org/repo/pull/42/") == 42
    assert parse_pr_number("https://github.com/org/repo") is None
    assert parse_pr_number("not a url") is None


def test_build_candidate_comment_states_nothing_was_published():
    task = Task(
        repository="jan21deepak/omnigent",
        issue_number=2,
        issue_title="[Bug] cold start",
        droid_session_id="sess-abc",
        status=TaskStatus.COMPLETED,
        summary="Fixed the guard clause and added tests.",
        duration_seconds=734.0,
        completed_at=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        candidate_sha="c" * 40,
        candidate_branch="droid/fix-2",
        candidate_status="ready",
    )
    comment = build_candidate_comment(task)
    assert "sess-abc" in comment
    assert "c" * 12 in comment
    assert "droid/fix-2" in comment
    assert "12m 14s" in comment
    assert "Fixed the guard clause" in comment
    assert "Nothing has been pushed and no pull request was opened" in comment


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
