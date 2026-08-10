from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import worker
from app.database import db_session
from app.models import ReviewTask, Task, TaskStatus


class StubCursor:
    """Minimal CursorClient stand-in returning a fixed run payload."""

    configured = True

    def __init__(self, run_payload, *, status=None, pr_url=None):
        self.run_payload = run_payload
        self._status = status
        self._pr_url = pr_url

    async def get_agent_status(self, agent_id, run_id=None):
        status = self._status
        if status is None:
            from app.cursor_client import map_run_status, extract_pull_request_url

            pr = self._pr_url or extract_pull_request_url(self.run_payload)
            status = map_run_status(self.run_payload.get("status"), bool(pr))
        pr_url = self._pr_url
        if pr_url is None:
            from app.cursor_client import extract_pull_request_url

            pr_url = extract_pull_request_url(self.run_payload)
        return status, pr_url, self.run_payload

    async def get_run_summary(self, agent_id, run_id=None):
        return self.run_payload.get("result") or "Fixed the bug."

    async def create_review_agent(self, **kwargs):
        raise AssertionError("review create not expected in this test")


class StubGitHub:
    def __init__(self, pull_requests=None, reviews=None):
        self.pull_requests = pull_requests or {}
        self.reviews = reviews or {}
        self.comments = []
        self.created_prs = []
        self.bugbot_requests = []

    async def post_issue_comment(self, repository, issue_number, body):
        self.comments.append((repository, issue_number, body))
        return True

    async def list_issue_comments(self, repository, issue_number, *, per_page=100):
        return [
            {"body": body}
            for repo, num, body in self.comments
            if repo == repository and num == issue_number
        ]

    async def request_bugbot_review(self, repository, pr_number):
        # Mirror production: post idempotent trigger comment
        existing = await self.list_issue_comments(repository, pr_number)
        if any((c.get("body") or "").strip().lower().startswith("bugbot run") for c in existing):
            return False
        await self.post_issue_comment(repository, pr_number, "bugbot run")
        self.bugbot_requests.append((repository, pr_number))
        return True

    async def get_pull_request(self, repository, pr_number):
        return self.pull_requests.get((repository, pr_number), {"state": "open"})

    async def merge_pull_request(self, *args, **kwargs):
        raise AssertionError("auto-merge should be disabled")

    async def enable_auto_merge(self, *args, **kwargs):
        raise AssertionError("auto-merge should be disabled")

    async def list_pull_request_reviews(self, repository, pr_number):
        return self.reviews.get((repository, pr_number), [])

    async def find_open_pull_request_for_head(self, repository, head):
        return None

    async def get_repository(self, full_name):
        return {"default_branch": "main", "full_name": full_name}

    async def create_pull_request(
        self, repository, *, title, head, base, body="", draft=False, same_repo=True
    ):
        owner = repository.split("/", 1)[0]
        head_ref = head
        if same_repo:
            branch = head.split(":", 1)[-1]
            head_ref = f"{owner}:{branch}"
        pr = {
            "number": 99,
            "html_url": f"https://github.com/{repository}/pull/99",
            "title": title,
            "head": {
                "ref": head_ref.split(":", 1)[-1],
                "repo": {"full_name": repository},
            },
            "base": {
                "ref": base,
                "repo": {"full_name": repository},
            },
            "body": body,
            "_head_param": head_ref,
        }
        self.created_prs.append(pr)
        return pr


def add_task(**overrides):
    defaults = dict(
        repository="jan21deepak/omnigent",
        repository_url="https://github.com/jan21deepak/omnigent",
        issue_number=2,
        issue_title="[Bug] cold start can call StartCascade",
        cursor_agent_id="bc-abc",
        cursor_run_id="run-abc",
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


async def _noop(*args, **kwargs):
    return None


async def _zero(*args, **kwargs):
    return 0


@pytest.mark.asyncio
async def test_failed_task_is_recovered_when_pr_appears_later(client, monkeypatch):
    """A failed agent can still open a PR afterwards — recover on recheck."""
    task_id = add_task()
    pr_url = "https://github.com/jan21deepak/omnigent/pull/7"
    cursor = StubCursor(
        {
            "id": "run-abc",
            "status": "FINISHED",
            "result": "Opened PR",
            "durationMs": 12000,
            "git": {"branches": [{"prUrl": pr_url}]},
        }
    )
    github = StubGitHub()
    monkeypatch.setattr(worker, "ensure_review_for_pr", _noop)
    monkeypatch.setattr(worker, "poll_reviews_once", _zero)

    await worker.poll_once(cursor=cursor, github=github)

    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.pull_request_url == pr_url
    assert github.comments, "completion comment should be posted on recovery"


@pytest.mark.asyncio
async def test_failed_task_without_pr_stays_failed(client, monkeypatch):
    task_id = add_task()
    cursor = StubCursor({"id": "run-abc", "status": "ERROR", "git": {"branches": []}})
    github = StubGitHub()
    monkeypatch.setattr(worker, "ensure_review_for_pr", _noop)
    monkeypatch.setattr(worker, "poll_reviews_once", _zero)

    await worker.poll_once(cursor=cursor, github=github)

    with db_session() as session:
        assert session.get(Task, task_id).status == TaskStatus.FAILED
    assert not github.comments


@pytest.mark.asyncio
async def test_pr_fallback_opens_pr_when_cursor_only_pushed_branch(client, monkeypatch):
    """When Cursor pushes a branch but cannot open a PR, forge opens it via PAT."""
    task_id = add_task(
        status=TaskStatus.RUNNING,
        completed_at=None,
        duration_seconds=None,
        issue_number=58,
        issue_title="[Bug] Android switcher height",
    )
    cursor = StubCursor(
        {
            "id": "run-abc",
            "status": "FINISHED",
            "result": (
                "Branch cursor/android-switcher-height-0e0c is pushed, but PR "
                "creation failed due to repository permissions (must be a collaborator)."
            ),
            "durationMs": 5000,
            "git": {
                "branches": [
                    {
                        "branch": "cursor/android-switcher-height-0e0c",
                        "prUrl": "",
                    }
                ]
            },
        }
    )
    github = StubGitHub()
    monkeypatch.setattr(worker, "ensure_review_for_pr", _noop)
    monkeypatch.setattr(worker, "poll_reviews_once", _zero)

    await worker.poll_once(cursor=cursor, github=github)

    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.pull_request_url == "https://github.com/jan21deepak/omnigent/pull/99"
    assert github.created_prs
    assert github.created_prs[0]["head"]["ref"] == "cursor/android-switcher-height-0e0c"
    assert github.created_prs[0]["_head_param"] == "jan21deepak:cursor/android-switcher-height-0e0c"
    assert github.created_prs[0]["base"]["repo"]["full_name"] == "jan21deepak/omnigent"
    assert github.bugbot_requests == [("jan21deepak/omnigent", 99)]
    assert any(body == "bugbot run" for _, _, body in github.comments)


@pytest.mark.asyncio
async def test_review_completion_does_not_auto_merge_by_default(client, monkeypatch):
    """REVIEW_AUTO_MERGE defaults to false so Bugbot can review before merge."""
    from app.config import get_settings

    get_settings.cache_clear()
    assert get_settings().review_auto_merge is False

    with db_session() as session:
        review = ReviewTask(
            repository="jan21deepak/omnigent",
            repository_url="https://github.com/jan21deepak/omnigent",
            pr_number=69,
            pr_title="Keep open for Bugbot",
            pr_url="https://github.com/jan21deepak/omnigent/pull/69",
            cursor_agent_id="bc-review",
            cursor_run_id="run-review",
            status=TaskStatus.RUNNING,
        )
        session.add(review)
        session.flush()
        review_id = review.id

    class DoneCursor(StubCursor):
        async def get_agent_status(self, agent_id, run_id=None):
            return TaskStatus.COMPLETED, None, {
                "id": "run-review",
                "status": "FINISHED",
                "result": "LGTM with notes",
                "durationMs": 1000,
            }

    github = StubGitHub()
    updated = await worker.poll_reviews_once(cursor=DoneCursor({}), github=github)
    assert updated == 1
    with db_session() as session:
        row = session.get(ReviewTask, review_id)
        assert row.status == TaskStatus.COMPLETED
        assert row.merged is False
        assert row.auto_merge_enabled is False


@pytest.mark.asyncio
async def test_old_failed_tasks_are_not_rechecked(client, monkeypatch):
    stale = datetime.now(timezone.utc) - timedelta(days=worker.RECHECK_FAILED_DAYS + 1)
    task_id = add_task(created_at=stale, completed_at=stale)

    class Exploding(StubCursor):
        async def get_agent_status(self, agent_id, run_id=None):
            raise AssertionError("stale failed task should not be polled")

    monkeypatch.setattr(worker, "ensure_review_for_pr", _noop)
    monkeypatch.setattr(worker, "poll_reviews_once", _zero)
    await worker.poll_once(cursor=Exploding({}), github=StubGitHub())

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


class MissingReviewCursor(StubCursor):
    """Cursor no longer knows about the review agent (404)."""

    async def get_agent_status(self, agent_id, run_id=None):
        request = httpx.Request("GET", "https://api.cursor.com/v1/agents/bc-x")
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
        cursor_agent_id="bc-review",
        cursor_run_id="run-review",
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
        cursor=MissingReviewCursor({}), github=StubGitHub()
    )

    assert updated == 1
    with db_session() as session:
        assert session.get(ReviewTask, review_id) is None


@pytest.mark.asyncio
async def test_abandoned_review_completes_when_cursor_bot_reviewed_on_github(client):
    review_id = add_review()
    github = StubGitHub(
        reviews={
            ("jan21deepak/omnigent", 6): [
                {
                    "user": {"login": "cursor[bot]"},
                    "submitted_at": "2026-08-02T14:00:00Z",
                },
                {
                    "user": {"login": "cursor[bot]"},
                    "submitted_at": "2026-08-02T14:08:00Z",
                },
            ]
        }
    )

    await worker.poll_reviews_once(cursor=MissingReviewCursor({}), github=github)

    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        assert review.status == TaskStatus.COMPLETED
        assert review.duration_seconds == 8 * 60
        assert review.completed_at is not None


@pytest.mark.asyncio
async def test_recent_missing_review_is_left_alone(client):
    review_id = add_review(created_at=datetime.now(timezone.utc))

    updated = await worker.poll_reviews_once(
        cursor=MissingReviewCursor({}), github=StubGitHub()
    )

    assert updated == 0
    with db_session() as session:
        assert session.get(ReviewTask, review_id).status == TaskStatus.QUEUED
