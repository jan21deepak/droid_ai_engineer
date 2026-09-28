"""Tests for event-driven Droid run finalization and GitHub state polling."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app import worker
from app.database import db_session
from app.models import ReviewTask, Task, TaskStatus


class StubGitHub:
    def __init__(self, pull_requests=None):
        self.pull_requests = pull_requests or {}
        self.comments = []
        self.created_prs = []
        self.review_posts = []

    async def post_issue_comment(self, repository, issue_number, body):
        self.comments.append((repository, issue_number, body))
        return True

    async def get_pull_request(self, repository, pr_number):
        return self.pull_requests.get((repository, pr_number), {"state": "open"})

    async def merge_pull_request(self, *args, **kwargs):
        raise AssertionError("auto-merge should be disabled by default")

    async def enable_auto_merge(self, *args, **kwargs):
        raise AssertionError("auto-merge should be disabled by default")

    async def get_repository(self, full_name):
        return {"default_branch": "main", "full_name": full_name}

    async def find_open_pull_request_for_head(self, repository, head):
        return None

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
            "head": {"ref": head_ref.split(":", 1)[-1], "repo": {"full_name": repository}},
            "base": {"ref": base, "repo": {"full_name": repository}},
            "body": body,
            "_head_param": head_ref,
        }
        self.created_prs.append(pr)
        return pr

    async def create_pull_request_review(self, repository, pr_number, body, *, event="COMMENT"):
        self.review_posts.append((repository, pr_number, body, event))
        return {"id": 1, "event": event}


class StubDroid:
    """DroidClient stand-in for finalize paths."""

    configured = True

    def __init__(self, branch="droid/fix-branch"):
        self.branch = branch
        self.resumed: list[str] = []
        self.pushed: list[str] = []

    @property
    def active_sessions(self):
        return set()

    async def current_branch(self, repository, task_id):
        return self.branch

    async def ensure_branch_pushed(self, repository, task_id, branch):
        self.pushed.append(branch)
        return True

    async def resume_interrupted(self, session_id, *, on_complete=None):
        self.resumed.append(session_id)
        return {"session_id": session_id, "status": "running"}


def add_task(**overrides):
    defaults = dict(
        repository="jan21deepak/omnigent",
        repository_url="https://github.com/jan21deepak/omnigent",
        issue_number=2,
        issue_title="[Bug] cold start can call StartCascade",
        droid_session_id="sess-abc",
        status=TaskStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    with db_session() as session:
        task = Task(**defaults)
        session.add(task)
        session.flush()
        return task.id


def success_outcome(text="Fixed the bug.\nBRANCH: droid/fix-branch", duration=734.0):
    return {
        "session_id": "sess-abc",
        "kind": "fix",
        "success": True,
        "subtype": "success",
        "text": text,
        "duration_seconds": duration,
        "credits": 2.5,
        "tokens": 12345,
        "error": None,
    }


def failure_outcome(error="droid crashed"):
    return {
        "session_id": "sess-abc",
        "kind": "fix",
        "success": False,
        "subtype": "error_during_execution",
        "text": "",
        "duration_seconds": 12.0,
        "credits": None,
        "tokens": None,
        "error": error,
    }


@pytest.mark.asyncio
async def test_finalize_fix_task_success_opens_pr_comments_and_starts_review(client, monkeypatch):
    task_id = add_task()
    github = StubGitHub()
    droid = StubDroid()
    started_reviews = []

    async def fake_start_pr_review(**kwargs):
        started_reviews.append(kwargs)
        return {"review_id": 77, "started": True, "detail": "droid_review_started"}

    monkeypatch.setattr(worker, "GitHubClient", lambda: github)
    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
    monkeypatch.setattr(worker, "start_pr_review", fake_start_pr_review)

    await worker.finalize_fix_task(task_id, success_outcome())

    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.pull_request_url == "https://github.com/jan21deepak/omnigent/pull/99"
        assert task.summary.startswith("Fixed the bug.")
        assert task.duration_seconds == 734.0
        assert task.factory_credits == 2.5
        assert task.estimated_tokens == 12345
        assert task.completed_at is not None

    assert github.created_prs, "forge should open the same-repo PR via PAT"
    assert github.created_prs[0]["_head_param"] == "jan21deepak:droid/fix-branch"
    assert github.created_prs[0]["base"]["repo"]["full_name"] == "jan21deepak/omnigent"
    assert github.comments, "completion comment should be posted on the issue"
    assert "sess-abc" in github.comments[0][2]
    # The just-created PR must appear in the comment, not "n/a".
    assert "https://github.com/jan21deepak/omnigent/pull/99" in github.comments[0][2]
    assert "n/a" not in github.comments[0][2]
    assert started_reviews, "PR review session should be started"
    assert started_reviews[0]["pr_number"] == 99


@pytest.mark.asyncio
async def test_finalize_fix_task_success_reuses_existing_pr(client, monkeypatch):
    task_id = add_task(
        pull_request_url="https://github.com/jan21deepak/omnigent/pull/7",
        pr_state="open",
    )
    github = StubGitHub()
    droid = StubDroid()
    started_reviews = []

    async def fake_start_pr_review(**kwargs):
        started_reviews.append(kwargs)
        return {"review_id": 78, "started": True, "detail": "droid_review_started"}

    monkeypatch.setattr(worker, "GitHubClient", lambda: github)
    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
    monkeypatch.setattr(worker, "start_pr_review", fake_start_pr_review)

    await worker.finalize_fix_task(task_id, success_outcome())

    assert not github.created_prs, "existing PR must not be duplicated"
    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.pull_request_url.endswith("/pull/7")
    assert started_reviews and started_reviews[0]["pr_number"] == 7


@pytest.mark.asyncio
async def test_finalize_fix_task_failure_marks_failed(client, monkeypatch):
    task_id = add_task()
    github = StubGitHub()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)
    monkeypatch.setattr(worker, "get_droid_client", lambda: StubDroid())

    await worker.finalize_fix_task(task_id, failure_outcome())

    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.FAILED
        assert "droid crashed" in task.error
    assert not github.comments
    assert not github.created_prs


def test_ensure_review_task_row_dedupes_active():
    first_id, created = worker.ensure_review_task_row(
        repository="jan21deepak/omnigent", pr_number=5
    )
    assert created is True
    second_id, created_again = worker.ensure_review_task_row(
        repository="jan21deepak/omnigent", pr_number=5
    )
    assert created_again is False
    assert second_id == first_id

    # A completed review does not block a fresh review request.
    with db_session() as session:
        session.get(ReviewTask, first_id).status = TaskStatus.COMPLETED
    third_id, created_third = worker.ensure_review_task_row(
        repository="jan21deepak/omnigent", pr_number=5
    )
    assert created_third is True
    assert third_id != first_id


@pytest.mark.asyncio
async def test_finalize_review_task_posts_github_review_with_verdict(client, monkeypatch):
    with db_session() as session:
        review = ReviewTask(
            repository="jan21deepak/omnigent",
            repository_url="https://github.com/jan21deepak/omnigent",
            pr_number=9,
            pr_title="Fix the thing",
            pr_url="https://github.com/jan21deepak/omnigent/pull/9",
            droid_session_id="sess-rev",
            status=TaskStatus.RUNNING,
        )
        session.add(review)
        session.flush()
        review_id = review.id

    github = StubGitHub()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)

    outcome = {
        "session_id": "sess-rev",
        "kind": "review",
        "success": True,
        "subtype": "success",
        "text": "Solid change with one nit.\nVERDICT: APPROVE",
        "duration_seconds": 240.0,
        "credits": 0.5,
        "tokens": None,
        "error": None,
    }
    await worker.finalize_review_task(review_id, outcome)

    assert github.review_posts == [
        ("jan21deepak/omnigent", 9, "Solid change with one nit.\nVERDICT: APPROVE", "APPROVE")
    ]
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        assert review.status == TaskStatus.COMPLETED
        assert review.duration_seconds == 240.0
        assert review.merged is False


@pytest.mark.asyncio
async def test_finalize_review_task_falls_back_to_comment_event(client, monkeypatch):
    with db_session() as session:
        review = ReviewTask(
            repository="jan21deepak/omnigent",
            pr_number=10,
            pr_title="Fix the other thing",
            pr_url="https://github.com/jan21deepak/omnigent/pull/10",
            droid_session_id="sess-rev2",
            status=TaskStatus.RUNNING,
        )
        session.add(review)
        session.flush()
        review_id = review.id

    class OwnPrGitHub(StubGitHub):
        async def create_pull_request_review(self, repository, pr_number, body, *, event="COMMENT"):
            if event != "COMMENT":
                # GitHub rejects APPROVE on the token owner's own PR.
                request = httpx.Request("POST", f"https://api.github.com/repos/{repository}/pulls/{pr_number}/reviews")
                raise httpx.HTTPStatusError(
                    "Can not approve your own pull request",
                    request=request,
                    response=httpx.Response(422, request=request),
                )
            return await super().create_pull_request_review(repository, pr_number, body, event=event)

    github = OwnPrGitHub()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)

    outcome = {
        "session_id": "sess-rev2",
        "kind": "review",
        "success": True,
        "subtype": "success",
        "text": "Needs changes.\nVERDICT: REQUEST_CHANGES",
        "duration_seconds": 120.0,
        "credits": None,
        "tokens": None,
        "error": None,
    }
    await worker.finalize_review_task(review_id, outcome)

    events = [post[3] for post in github.review_posts]
    assert events == ["COMMENT"], "own-PR rejection should fall back to COMMENT"


@pytest.mark.asyncio
async def test_recovery_resumes_orphaned_sessions_and_fails_lost_rows(client, monkeypatch):
    running_id = add_task(droid_session_id="sess-orphan")
    queued_id = add_task(
        repository="jan21deepak/omnigent",
        issue_number=3,
        droid_session_id=None,
        status=TaskStatus.QUEUED,
    )
    with db_session() as session:
        review = ReviewTask(
            repository="jan21deepak/omnigent",
            pr_number=6,
            pr_url="https://github.com/jan21deepak/omnigent/pull/6",
            droid_session_id=None,
            status=TaskStatus.QUEUED,
        )
        session.add(review)
        session.flush()
        review_id = review.id

    droid = StubDroid()
    recovered = await worker.recover_interrupted_tasks(droid)
    assert recovered == 1
    assert droid.resumed == ["sess-orphan"]

    with db_session() as session:
        assert session.get(Task, running_id).status == TaskStatus.RUNNING
        assert session.get(Task, queued_id).status == TaskStatus.FAILED
        assert "restart" in session.get(Task, queued_id).error
        assert session.get(ReviewTask, review_id).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_recovery_skips_live_sessions(client):
    task_id = add_task(droid_session_id="sess-live")

    class LiveDroid(StubDroid):
        @property
        def active_sessions(self):
            return {"sess-live"}

    recovered = await worker.recover_interrupted_tasks(LiveDroid())
    assert recovered == 0
    with db_session() as session:
        assert session.get(Task, task_id).status == TaskStatus.RUNNING


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
    github = StubGitHub({("jan21deepak/omnigent", 7): {"state": "open", "merged": False}})
    with db_session() as session:
        tasks = [session.get(Task, task_id)]

    await worker.refresh_pr_states(github, tasks)

    with db_session() as session:
        assert session.get(Task, task_id).pr_state == "open"


@pytest.mark.asyncio
async def test_poll_github_state_marks_merged_reviews(client):
    with db_session() as session:
        review = ReviewTask(
            repository="jan21deepak/omnigent",
            pr_number=12,
            pr_title="Merged elsewhere",
            pr_url="https://github.com/jan21deepak/omnigent/pull/12",
            status=TaskStatus.COMPLETED,
            merged=False,
        )
        session.add(review)
        session.flush()
        review_id = review.id

    github = StubGitHub(
        {("jan21deepak/omnigent", 12): {"state": "closed", "merged": True, "merged_at": "2026-08-02T10:00:00Z"}}
    )
    updated = await worker.poll_github_state(github)
    assert updated == 1
    with db_session() as session:
        assert session.get(ReviewTask, review_id).merged is True


@pytest.mark.asyncio
async def test_finalize_merged_running_task_completes_active_rows(client):
    task_id = add_task(
        status=TaskStatus.RUNNING,
        pull_request_url="https://github.com/jan21deepak/omnigent/pull/4",
        pr_state="merged",
    )
    updated = await worker.finalize_merged_running_tasks()
    assert updated == 1
    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.completed_at is not None
