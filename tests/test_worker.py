"""Tests for event-driven Droid run finalization and GitHub state polling."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app import worker
from app.candidates import CandidateStatus, WorkspaceState
from app.config import get_settings
from app.database import db_session
from app.models import ReviewTask, Task, TaskStatus
from app.review_policy import REVIEW_POLICY_RESTRICTED, ReviewContext, ReviewVerdict

HEAD = "b" * 40
BASE = "a" * 40


class StubGitHub:
    def __init__(self, pull_requests=None):
        self.pull_requests = pull_requests or {}
        self.comments = []
        self.created_prs = []
        self.review_posts = []
        self.calls = []

    def __getattr__(self, name):
        # Any GitHub method a test did not expect (e.g. a merge) is recorded and fails loudly.
        async def unexpected(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"unexpected GitHub call: {name}")

        return unexpected

    async def post_issue_comment(self, repository, issue_number, body):
        self.comments.append((repository, issue_number, body))
        return True

    async def get_pull_request(self, repository, pr_number):
        self.calls.append("get_pull_request")
        return self.pull_requests.get((repository, pr_number), {"state": "open", "head": {"sha": HEAD}})

    async def get_repository(self, full_name):
        return {"default_branch": "main", "full_name": full_name}

    async def create_pull_request(self, repository, **kwargs):
        self.created_prs.append(kwargs)
        return {"number": 99, "html_url": f"https://github.com/{repository}/pull/99"}

    async def create_pull_request_review(self, repository, pr_number, body, *, event="COMMENT", commit_id=None):
        self.review_posts.append((repository, pr_number, body, event, commit_id))
        return {"id": 1, "event": event}


class StubDroid:
    """DroidClient stand-in for finalize/recovery paths."""

    configured = True

    def __init__(self, head_sha=HEAD, branch="droid/fix-branch", dirty=False, live=()):
        self.state = WorkspaceState(head_sha=head_sha, branch=branch, dirty=dirty)
        self.live = set(live)
        self.inspected: list[tuple] = []
        self.resumed: list[str] = []
        self.pushed: list[str] = []
        self.review_runs: list[dict] = []
        self.context_requests: list[dict] = []
        self.context = None

    @property
    def active_sessions(self):
        return self.live

    def track(self, key, task):
        return task

    async def inspect_workspace(self, repository, task_id, reported_branch):
        self.inspected.append((repository, task_id, reported_branch))
        return self.state

    async def ensure_branch_pushed(self, repository, task_id, branch):
        self.pushed.append(branch)
        return True

    async def resume_interrupted(self, session_id, *, cwd=None, on_complete=None):
        self.resumed.append(session_id)
        return {"session_id": session_id, "status": "running"}

    async def prepare_review_context(self, **kwargs):
        self.context_requests.append(kwargs)
        return self.context

    async def start_review_run(self, **kwargs):
        self.review_runs.append(kwargs)
        return {"session_id": "sess-review", "status": "running"}


@pytest.fixture
def github_writes(monkeypatch):
    """Enable every permitted GitHub write so tests prove finalization still publishes nothing."""
    monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "true")
    monkeypatch.setenv("FORGE_ALLOWED_BRANCHES", "droid/fix-branch")
    monkeypatch.setenv("REVIEW_AUTO_MERGE", "true")
    get_settings.cache_clear()


def add_task(**overrides):
    defaults = {
        "repository": "jan21deepak/omnigent",
        "repository_url": "https://github.com/jan21deepak/omnigent",
        "issue_number": 2,
        "issue_title": "[Bug] cold start can call StartCascade",
        "droid_session_id": "sess-abc",
        "status": TaskStatus.RUNNING,
        "base_sha": BASE,
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    with db_session() as session:
        task = Task(**defaults)
        session.add(task)
        session.flush()
        return task.id


def add_review(**overrides):
    values = {
        "repository": "jan21deepak/omnigent",
        "repository_url": "https://github.com/jan21deepak/omnigent",
        "pr_number": 9,
        "pr_title": "Fix the thing",
        "pr_url": "https://github.com/jan21deepak/omnigent/pull/9",
        "droid_session_id": "sess-rev",
        "status": TaskStatus.RUNNING,
        "commit_sha": HEAD,
        "base_sha": BASE,
        "diff_complete": True,
        "review_policy": REVIEW_POLICY_RESTRICTED,
    }
    values.update(overrides)
    with db_session() as session:
        review = ReviewTask(**values)
        session.add(review)
        session.flush()
        return review.id


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


def review_outcome(verdict="APPROVE", findings=None):
    payload = {"verdict": verdict, "summary": "Assessment.", "findings": findings or []}
    text = f"Notes.\n```json\n{json.dumps(payload)}\n```\nVERDICT: {verdict}"
    return {
        "session_id": "sess-rev",
        "kind": "review",
        "success": True,
        "subtype": "success",
        "text": text,
        "duration_seconds": 240.0,
        "credits": 0.5,
        "tokens": None,
        "error": None,
    }


@pytest.fixture
def no_reviews(monkeypatch):
    started = []

    async def fake_start_pr_review(**kwargs):
        started.append(kwargs)
        return {"review_id": 77, "started": True, "detail": "droid_review_started"}

    monkeypatch.setattr(worker, "start_pr_review", fake_start_pr_review)
    return started


@pytest.mark.asyncio
async def test_finalize_success_records_candidate_and_publishes_nothing(client, monkeypatch, github_writes, no_reviews):
    task_id = add_task()
    github = StubGitHub()
    droid = StubDroid()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)
    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)

    await worker.finalize_fix_task(task_id, success_outcome())

    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.summary.startswith("Fixed the bug.")
        assert (task.duration_seconds, task.factory_credits, task.estimated_tokens) == (734.0, 2.5, 12345)
        assert task.candidate_sha == HEAD
        assert task.candidate_branch == "droid/fix-branch"
        assert task.candidate_status == CandidateStatus.READY
        assert task.candidate_recorded_at is not None
        assert task.pull_request_url is None
        payload = task.to_dict()
    assert payload["candidate"]["sha"] == HEAD
    assert droid.inspected == [("jan21deepak/omnigent", task_id, "droid/fix-branch")]
    # Agent success alone: no push, no PR, no review, no merge.
    assert droid.pushed == []
    assert github.created_prs == []
    assert no_reviews == []
    assert github.calls == []
    [(_repo, _issue, comment)] = github.comments
    assert "Nothing has been pushed" in comment and "pull/" not in comment


@pytest.mark.asyncio
async def test_follow_up_success_on_existing_pr_does_not_push_or_review(client, monkeypatch, github_writes, no_reviews):
    task_id = add_task(pull_request_url="https://github.com/jan21deepak/omnigent/pull/7", pr_state="open")
    github = StubGitHub()
    droid = StubDroid()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)
    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)

    await worker.finalize_fix_task(task_id, success_outcome())

    assert droid.pushed == []
    assert github.created_prs == []
    assert no_reviews == []
    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.pull_request_url.endswith("/pull/7")
        assert task.candidate_sha == HEAD


@pytest.mark.asyncio
async def test_new_candidate_replaces_old_sha_and_classifies_state(client, monkeypatch):
    task_id = add_task()
    monkeypatch.setattr(worker, "GitHubClient", lambda: StubGitHub())

    first = StubDroid(head_sha="c" * 40)
    monkeypatch.setattr(worker, "get_droid_client", lambda: first)
    await worker.finalize_fix_task(task_id, success_outcome())
    second = StubDroid(head_sha="d" * 40, dirty=True)
    monkeypatch.setattr(worker, "get_droid_client", lambda: second)
    await worker.finalize_fix_task(task_id, success_outcome())
    with db_session() as session:
        task = session.get(Task, task_id)
        assert (task.candidate_sha, task.candidate_status) == ("d" * 40, CandidateStatus.DIRTY)

    unchanged = StubDroid(head_sha=BASE)
    monkeypatch.setattr(worker, "get_droid_client", lambda: unchanged)
    await worker.finalize_fix_task(task_id, success_outcome())
    with db_session() as session:
        assert session.get(Task, task_id).candidate_status == CandidateStatus.NO_CHANGES


@pytest.mark.asyncio
async def test_finalize_fix_task_failure_marks_failed(client, monkeypatch):
    task_id = add_task()
    github = StubGitHub()
    droid = StubDroid()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)
    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)

    await worker.finalize_fix_task(task_id, failure_outcome())

    with db_session() as session:
        task = session.get(Task, task_id)
        assert task.status == TaskStatus.FAILED
        assert "droid crashed" in task.error
        assert task.candidate_sha is None
    assert droid.inspected == []
    assert not github.comments
    assert not github.created_prs


def test_ensure_review_task_row_dedupes_active():
    first_id, created = worker.ensure_review_task_row(repository="jan21deepak/omnigent", pr_number=5)
    assert created is True
    second_id, created_again = worker.ensure_review_task_row(repository="jan21deepak/omnigent", pr_number=5)
    assert created_again is False
    assert second_id == first_id

    # A completed review does not block a fresh review request.
    with db_session() as session:
        session.get(ReviewTask, first_id).status = TaskStatus.COMPLETED
    third_id, created_third = worker.ensure_review_task_row(repository="jan21deepak/omnigent", pr_number=5)
    assert created_third is True
    assert third_id != first_id


@pytest.mark.asyncio
async def test_restricted_complete_review_posts_approval_pinned_to_reviewed_sha(client, monkeypatch, github_writes):
    review_id = add_review()
    github = StubGitHub()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)

    await worker.finalize_review_task(
        review_id, review_outcome(findings=[{"severity": "nit", "title": "Rename x", "file": "x.py", "line": 3}])
    )

    [(repo, pr, body, event, commit_id)] = github.review_posts
    assert (repo, pr, event, commit_id) == ("jan21deepak/omnigent", 9, "APPROVE", HEAD)
    assert "VERDICT: APPROVE" in body
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        assert review.status == TaskStatus.COMPLETED
        assert review.duration_seconds == 240.0
        assert review.verdict == ReviewVerdict.APPROVE
        assert review.merged is False
        assert review.auto_merge_enabled is False
        payload = review.to_dict()
    assert payload["findings"][0]["title"] == "Rename x"
    assert payload["review_policy"] == REVIEW_POLICY_RESTRICTED
    # REVIEW_AUTO_MERGE=true is ignored: no merge or auto-merge call was attempted.
    assert github.calls == ["get_pull_request"]


@pytest.mark.parametrize(
    "overrides",
    [{"diff_complete": False}, {"diff_complete": None}, {"review_policy": None}, {"commit_sha": None}],
    ids=["incomplete_diff", "unknown_diff", "legacy_unrestricted", "unknown_sha"],
)
@pytest.mark.asyncio
async def test_approval_without_complete_restricted_context_is_not_an_approval(
    client, monkeypatch, github_writes, overrides
):
    review_id = add_review(**overrides)
    github = StubGitHub()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)

    await worker.finalize_review_task(review_id, review_outcome())

    assert [post[3] for post in github.review_posts] == ["COMMENT"]
    with db_session() as session:
        assert session.get(ReviewTask, review_id).verdict == ReviewVerdict.INCONCLUSIVE


@pytest.mark.asyncio
async def test_approval_is_downgraded_when_pr_head_moved(client, monkeypatch, github_writes):
    review_id = add_review()
    github = StubGitHub({("jan21deepak/omnigent", 9): {"state": "open", "head": {"sha": "e" * 40}}})
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)

    await worker.finalize_review_task(review_id, review_outcome())

    assert [post[3] for post in github.review_posts] == ["COMMENT"]
    with db_session() as session:
        # The AI verdict for the reviewed SHA is kept; it is just not posted as approval of a different head.
        review = session.get(ReviewTask, review_id)
        assert (review.verdict, review.commit_sha) == (ReviewVerdict.APPROVE, HEAD)


@pytest.mark.asyncio
async def test_finalize_review_task_falls_back_to_comment_event(client, monkeypatch, github_writes):
    review_id = add_review(pr_number=10)

    class OwnPrGitHub(StubGitHub):
        async def create_pull_request_review(self, repository, pr_number, body, *, event="COMMENT", commit_id=None):
            if event != "COMMENT":
                # GitHub rejects REQUEST_CHANGES on the token owner's own PR.
                request = httpx.Request("POST", f"https://api.github.com/repos/{repository}/pulls/{pr_number}/reviews")
                raise httpx.HTTPStatusError(
                    "Can not request changes on your own pull request",
                    request=request,
                    response=httpx.Response(422, request=request),
                )
            return await super().create_pull_request_review(
                repository, pr_number, body, event=event, commit_id=commit_id
            )

    github = OwnPrGitHub()
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)

    await worker.finalize_review_task(
        review_id, review_outcome("REQUEST_CHANGES", [{"severity": "major", "title": "Bug"}])
    )

    assert [(post[3], post[4]) for post in github.review_posts] == [("COMMENT", HEAD)]
    with db_session() as session:
        assert session.get(ReviewTask, review_id).verdict == ReviewVerdict.REQUEST_CHANGES


@pytest.mark.asyncio
async def test_launch_review_refuses_incomplete_diff(client, monkeypatch):
    review_id = add_review(
        status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, diff_complete=None, review_policy=None
    )
    droid = StubDroid()
    droid.context = ReviewContext(
        workspace=Path("/tmp/r"),
        base_ref="main",
        base_sha=BASE,
        head_sha=HEAD,
        diff="x" * 10,
        incomplete_reason="diff is 10 characters, above the 5-character review limit",
    )
    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
    monkeypatch.setattr(worker, "GitHubClient", lambda: StubGitHub())

    await worker.launch_review_task(
        review_id, repository="jan21deepak/omnigent", repository_url="", pr_number=9, pr_url=""
    )

    assert droid.review_runs == []
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        assert review.status == TaskStatus.FAILED
        assert "review not started" in review.error and "review limit" in review.error
        assert (review.diff_complete, review.commit_sha, review.base_sha) == (False, HEAD, BASE)


@pytest.mark.asyncio
async def test_launch_review_starts_fresh_restricted_session_with_exact_context(client, monkeypatch):
    review_id = add_review(
        status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, diff_complete=None, review_policy=None
    )
    droid = StubDroid()
    droid.context = ReviewContext(workspace=Path("/tmp/r"), base_ref="release", base_sha=BASE, head_sha=HEAD, diff="+x")
    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
    pr = {"state": "open", "head": {"sha": HEAD}, "base": {"ref": "release"}}
    monkeypatch.setattr(worker, "GitHubClient", lambda: StubGitHub({("jan21deepak/omnigent", 9): pr}))

    await worker.launch_review_task(
        review_id, repository="jan21deepak/omnigent", repository_url="", pr_number=9, pr_url=""
    )

    # The diff base is the PR's own target branch, not the repository default.
    assert [req["base_ref"] for req in droid.context_requests] == ["release"]
    [run] = droid.review_runs
    assert run["context"] is droid.context
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        assert review.status == TaskStatus.RUNNING
        assert review.droid_session_id == "sess-review"
        assert (review.commit_sha, review.base_sha, review.diff_complete) == (HEAD, BASE, True)
        assert review.review_policy == REVIEW_POLICY_RESTRICTED


@pytest.mark.asyncio
async def test_recovery_resumes_fix_sessions_but_never_reviews(client, monkeypatch):
    running_id = add_task(droid_session_id="sess-orphan")
    queued_id = add_task(issue_number=3, droid_session_id=None, status=TaskStatus.QUEUED)
    orphan_review = add_review(droid_session_id="sess-old-review")
    queued_review = add_review(pr_number=6, droid_session_id=None, status=TaskStatus.QUEUED)

    droid = StubDroid()
    recovered = await worker.recover_interrupted_tasks(droid)
    assert recovered == 1
    assert droid.resumed == ["sess-orphan"]
    assert droid.review_runs == []

    with db_session() as session:
        assert session.get(Task, running_id).status == TaskStatus.RUNNING
        assert session.get(Task, queued_id).status == TaskStatus.FAILED
        assert "restart" in session.get(Task, queued_id).error
        old = session.get(ReviewTask, orphan_review)
        assert old.status == TaskStatus.FAILED
        assert "never resumed" in old.error
        assert old.completed_at is not None
        assert session.get(ReviewTask, queued_review).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_recovery_closes_orphaned_reviews_even_without_droid(client):
    review_id = add_review(droid_session_id="sess-old-review")
    droid = StubDroid()
    droid.configured = False
    assert await worker.recover_interrupted_tasks(droid) == 0
    assert droid.resumed == []
    with db_session() as session:
        assert session.get(ReviewTask, review_id).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_recovery_skips_live_sessions(client):
    task_id = add_task(droid_session_id="sess-live")
    review_id = add_review(droid_session_id="sess-live-review")

    recovered = await worker.recover_interrupted_tasks(StubDroid(live={"sess-live", "sess-live-review"}))
    assert recovered == 0
    with db_session() as session:
        assert session.get(Task, task_id).status == TaskStatus.RUNNING
        assert session.get(ReviewTask, review_id).status == TaskStatus.RUNNING


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
async def test_poller_never_merges_or_enables_auto_merge(client, monkeypatch, github_writes):
    """Approved, legacy auto-merge-flagged rows with REVIEW_AUTO_MERGE=true are only observed."""
    review_id = add_review(status=TaskStatus.COMPLETED, verdict=ReviewVerdict.APPROVE, auto_merge_enabled=True)
    add_review(pr_number=11, status=TaskStatus.COMPLETED, verdict="APPROVE", review_policy=None, merged=False)
    github = StubGitHub()

    assert await worker.poll_github_state(github) == 0

    assert set(github.calls) == {"get_pull_request"}
    with db_session() as session:
        assert session.get(ReviewTask, review_id).merged is False


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
