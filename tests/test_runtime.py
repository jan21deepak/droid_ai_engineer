from datetime import datetime, timezone

from app.worker import (
    IDLE_GAP_SECONDS,
    review_runtime_from_github_reviews,
    session_active_runtime_seconds,
)


def test_session_active_runtime_excludes_user_waits_and_long_idle():
    t0 = 1_000_000.0
    messages = [
        {"created_at": t0, "source": "user", "message": "please fix"},
        {"created_at": t0 + 60, "source": "assistant", "message": "on it"},
        {"created_at": t0 + 600, "source": "assistant", "message": "PR opened"},
        # Human comes back 30 minutes later — must not count.
        {"created_at": t0 + 600 + 30 * 60, "source": "user", "message": "try now"},
        {"created_at": t0 + 600 + 30 * 60 + 45, "source": "assistant", "message": "pushed"},
        # Resume almost 10 hours later — must not count.
        {"created_at": t0 + 600 + 30 * 60 + 45 + 10 * 3600, "source": "user", "message": "Resume"},
        {
            "created_at": t0 + 600 + 30 * 60 + 45 + 10 * 3600 + 10,
            "source": "assistant",
            "message": "already done",
        },
    ]
    # Active = 60 (first reply) + 540 (work to PR) + 45 (reply after human)
    # + 10 (ack after resume). The 30m human wait and 10h idle are excluded.
    assert session_active_runtime_seconds(messages) == 655.0


def test_session_active_runtime_raw_span_would_be_inflated():
    t0 = 1_000_000.0
    messages = [
        {"created_at": t0, "source": "user"},
        {"created_at": t0 + 600, "source": "assistant"},
        {"created_at": t0 + 600 + IDLE_GAP_SECONDS + 1, "source": "assistant"},
    ]
    # Second agent gap is idle (>15m) so only the first 600s count.
    assert session_active_runtime_seconds(messages) == 600.0
    raw_span = messages[-1]["created_at"] - messages[0]["created_at"]
    assert raw_span > IDLE_GAP_SECONDS


def test_session_active_runtime_handles_millisecond_timestamps():
    t0 = 1_000_000_000_000.0  # ms
    messages = [
        {"created_at": t0, "source": "user"},
        {"created_at": t0 + 120_000, "source": "assistant"},
    ]
    assert session_active_runtime_seconds(messages) == 120.0


def test_session_active_runtime_needs_two_messages():
    assert session_active_runtime_seconds([]) is None
    assert session_active_runtime_seconds([{"created_at": 1, "source": "user"}]) is None


def test_review_runtime_from_github_span():
    reviews = [
        {
            "user": {"login": "alice"},
            "submitted_at": "2026-08-02T14:00:00Z",
        },
        {
            "user": {"login": "cursor[bot]"},
            "submitted_at": "2026-08-02T14:10:00Z",
        },
        {
            "user": {"login": "cursor[bot]"},
            "submitted_at": "2026-08-02T14:25:00Z",
        },
    ]
    assert review_runtime_from_github_reviews(reviews) == 15 * 60


def test_review_runtime_uses_api_created_at_for_single_comment():
    reviews = [
        {
            "user": {"login": "cursor[bot]"},
            "submitted_at": "2026-08-02T13:48:51Z",
        }
    ]
    created = datetime(2026, 8, 2, 13, 47, 35, tzinfo=timezone.utc)
    assert review_runtime_from_github_reviews(reviews, created) == 76.0


def test_review_runtime_ignores_non_cursor_reviews():
    reviews = [
        {"user": {"login": "alice"}, "submitted_at": "2026-08-02T14:00:00Z"},
        {"user": {"login": "bob"}, "submitted_at": "2026-08-02T15:00:00Z"},
    ]
    assert review_runtime_from_github_reviews(reviews) is None
