from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import worker
from app.database import db_session
from app.models import ReviewTask, Task, TaskStatus


class StubDevin:
    """Minimal DevinClient stand-in returning a fixed session payload."""

    configured = True

    def __init__(self, session_payload):
        self.session_payload = session_payload

    async def get_session(self, session_id):
        return self.session_payload

    async def get_session_messages(self, session_id):
        return []

    async def get_session_summary(self, session_id):
        return "Fixed the bug."

    async def get_pr_review(self, pr_url, commit_sha=None):
        raise AssertionError("review lookup not expected in this test")


class StubGitHub:
    def __init__(self, pull_requests=None, reviews=None):
        self.pull_requests = pull_requests or {}
        self.reviews = reviews or {}
        self.comments = []

    async def post_issue_comment(self, repository, issue_number, body):
        self.comments.append((repository, issue_number, body))

    async def get_pull_request(self, repository, pr_number):
        return self.pull_requests.get((repository, pr_number), {"state": "open"})

    async def list_pull_request_reviews(self, repository, pr_number):
        return self.reviews.get((repository, pr_number), [])


def add_task(**overrides):
    defaults = dict(
        repository="jan21deepak/omnigent",
        repository_url="https://github.com/jan21deepak/omnigent",
        issue_number=2,
        issue_title="[Bug] cold start can call StartCascade",
        devin_session_id="session-abc",
        status=TaskStatus.FAILED,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=719.0,
    )
    defaults.update(overrides)
    with db_session() as session:
        task = Task(**defaults)
        session.add(task)
        session.flush()
        return task.id


@pytest.mark.asyncio
async def test_failed_task_is_recovered_when_pr_appears_later(client, monkeypatch):
    """A session suspended for inactivity can still open a PR afterwards."""
    task_id = add_task()
    pr_url = "https://github.com/jan21deepak/omnigent/pull/7"
    devin = StubDevin(
        {
            "status": "suspended",
            "status_detail": "inactivity",
            "pull_requests": [{"pr_url": pr_url, "pr_state": "open"}],
            "acus_consumed": 0.0,
        }
    )
    github = StubGitHub()
    monkeypatch.setattr(worker, "ensure_review_for_pr", _noop)
    monkeypatch.setattr(worker, "poll_reviews_once", _zero)

    await worker.poll_once(devin=devin, github=github)

    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.pull_request_url == pr_url
    assert github.comments, "completion comment should be posted on recovery"


@pytest.mark.asyncio
async def test_failed_task_without_pr_stays_failed(client, monkeypatch):
    task_id = add_task()
    devin = StubDevin(
        {"status": "suspended", "status_detail": "inactivity", "pull_requests": []}
    )
    github = StubGitHub()
    monkeypatch.setattr(worker, "ensure_review_for_pr", _noop)
    monkeypatch.setattr(worker, "poll_reviews_once", _zero)

    await worker.poll_once(devin=devin, github=github)

    with db_session() as session:
        assert session.get(Task, task_id).status == TaskStatus.FAILED
    assert not github.comments


@pytest.mark.asyncio
async def test_old_failed_tasks_are_not_rechecked(client, monkeypatch):
    stale = datetime.now(timezone.utc) - timedelta(days=worker.RECHECK_FAILED_DAYS + 1)
    task_id = add_task(created_at=stale, completed_at=stale)

    class Exploding(StubDevin):
        async def get_session(self, session_id):
            raise AssertionError("stale failed task should not be polled")

    monkeypatch.setattr(worker, "ensure_review_for_pr", _noop)
    monkeypatch.setattr(worker, "poll_reviews_once", _zero)
    await worker.poll_once(devin=Exploding({}), github=StubGitHub())

    with db_session() as session:
        assert session.get(Task, task_id).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_refresh_pr_states_reads_merge_state_from_github(client):
    task_id = add_task(
        status=TaskStatus.COMPLETED,
        pull_request_url="https://github.com/jan21deepak/superset/pull/3",
        repository="jan21deepak/superset",
    )
    github = StubGitHub(
        {
            ("jan21deepak/superset", 3): {
                "state": "closed",
                "merged": True,
                "merged_at": "2026-08-02T14:30:47Z",
            }
        }
    )
    with db_session() as session:
        tasks = [session.get(Task, task_id)]

    updated = await worker.refresh_pr_states(github, tasks)

    assert updated == 1
    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.pr_state == "merged"
        assert task.pr_merged_at is not None


@pytest.mark.asyncio
async def test_refresh_pr_states_marks_open_pr(client):
    task_id = add_task(
        status=TaskStatus.COMPLETED,
        pull_request_url="https://github.com/jan21deepak/omnigent/pull/7",
    )
    github = StubGitHub(
        {("jan21deepak/omnigent", 7): {"state": "open", "merged": False}}
    )
    with db_session() as session:
        tasks = [session.get(Task, task_id)]

    await worker.refresh_pr_states(github, tasks)

    with db_session() as session:
        assert session.get(Task, task_id).pr_state == "open"


class MissingReviewDevin(StubDevin):
    """Devin no longer knows about the review it once reported (404)."""

    async def get_pr_review(self, pr_url, commit_sha=None):
        request = httpx.Request("GET", "https://api.devin.ai/v3/pr-reviews")
        raise httpx.HTTPStatusError(
            "not found",
            request=request,
            response=httpx.Response(404, request=request),
        )


def add_review(**overrides):
    defaults = dict(
        repository="jan21deepak/omnigent",
        repository_url="https://github.com/jan21deepak/omnigent",
        pr_number=6,
        pr_title="[Bug] shell tool gates are inert",
        pr_url="https://github.com/jan21deepak/omnigent/pull/6",
        status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc)
        - timedelta(hours=worker.ABANDONED_REVIEW_HOURS + 1),
    )
    defaults.update(overrides)
    with db_session() as session:
        review = ReviewTask(**defaults)
        session.add(review)
        session.flush()
        return review.id


@pytest.mark.asyncio
async def test_abandoned_review_without_github_evidence_is_discarded(client):
    review_id = add_review()

    updated = await worker.poll_reviews_once(
        devin=MissingReviewDevin({}), github=StubGitHub()
    )

    assert updated == 1
    with db_session() as session:
        assert session.get(ReviewTask, review_id) is None


@pytest.mark.asyncio
async def test_abandoned_review_completes_when_devin_bot_reviewed_on_github(client):
    review_id = add_review()
    github = StubGitHub(
        reviews={
            ("jan21deepak/omnigent", 6): [
                {
                    "user": {"login": "devin-ai-integration[bot]"},
                    "submitted_at": "2026-08-02T14:00:00Z",
                },
                {
                    "user": {"login": "devin-ai-integration[bot]"},
                    "submitted_at": "2026-08-02T14:08:00Z",
                },
            ]
        }
    )

    await worker.poll_reviews_once(devin=MissingReviewDevin({}), github=github)

    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        assert review.status == TaskStatus.COMPLETED
        assert review.duration_seconds == 8 * 60
        assert review.completed_at is not None


@pytest.mark.asyncio
async def test_recent_missing_review_is_left_alone(client):
    review_id = add_review(created_at=datetime.now(timezone.utc))

    updated = await worker.poll_reviews_once(
        devin=MissingReviewDevin({}), github=StubGitHub()
    )

    assert updated == 0
    with db_session() as session:
        assert session.get(ReviewTask, review_id).status == TaskStatus.QUEUED


async def _noop(*args, **kwargs):
    return None


async def _zero(*args, **kwargs):
    return 0
