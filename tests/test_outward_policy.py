"""Repository, ref and outward-action policy across every entry and background path.

Denied paths must never reach Git or GitHub. ``outbound`` records every HTTP
request any ``httpx.AsyncClient`` would send, so "zero outbound calls" is
asserted at the transport boundary rather than on individual methods.
"""

from datetime import UTC, datetime

import httpx
import pytest

from app import droid_client, worker
from app.config import get_settings
from app.database import db_session
from app.droid_client import DroidClient, task_workspace_dir
from app.github import GitHubClient
from app.models import Repository, ReviewTask, SyncedIssue, Task, TaskStatus
from app.policy import (
    OutwardAction,
    OutwardActionDeniedError,
    outward_decision,
    ref_decision,
    repository_decision,
)
from app.tasks import TaskCreateError, create_and_dispatch_task

ALLOWED = "jan21deepak/allowed"
OTHER = "someone/else"


def configure(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture
def outbound(monkeypatch):
    """Record (and fail) every outbound HTTP request."""
    sent: list[httpx.Request] = []

    async def record(self, request, *args, **kwargs):
        sent.append(request)
        return httpx.Response(599, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", record)
    return sent


@pytest.fixture
def git_calls(monkeypatch):
    calls: list[list[str]] = []

    async def fake_git(args, **kwargs):
        calls.append(list(args))
        return ""

    monkeypatch.setattr(droid_client, "_git", fake_git)
    return calls


@pytest.fixture
def allowlist(monkeypatch):
    configure(monkeypatch, FORGE_ALLOWED_REPOSITORIES=ALLOWED, GITHUB_TOKEN="ghp_" + "a" * 36)


class RecordingGitHub:
    """GitHub stand-in that records every call made by worker paths."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        async def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "get_repository":
                return {"default_branch": "main"}
            if name == "find_open_pull_request_for_head":
                return None
            if name == "get_pull_request":
                return {"state": "open"}
            if name == "create_pull_request":
                return {"number": 5, "html_url": f"https://github.com/{args[0]}/pull/5"}
            return True

        return method


class RecordingDroid:
    configured = True

    def __init__(self, branch="droid/fix-branch"):
        self.branch = branch
        self.pushed: list[str] = []
        self.started: list[dict] = []
        self.resumed: list[tuple] = []
        self.follow_ups: list[tuple] = []

    @property
    def active_sessions(self):
        return set()

    def track(self, key, task):
        return task

    async def current_branch(self, repository, task_id):
        return self.branch

    async def ensure_branch_pushed(self, repository, task_id, branch):
        self.pushed.append(branch)
        return True

    async def start_fix_run(self, **kwargs):
        self.started.append(kwargs)
        return {"session_id": "sess-new", "status": "running", "setup": "not_configured"}

    async def start_review_run(self, **kwargs):
        self.started.append(kwargs)
        return {"session_id": "sess-review", "status": "running"}

    async def resume_interrupted(self, session_id, *, cwd=None, on_complete=None):
        self.resumed.append((session_id, cwd))
        return {"session_id": session_id, "status": "running"}

    async def send_follow_up(self, session_id, instruction, *, cwd=None, on_complete=None):
        self.follow_ups.append((session_id, instruction, cwd))
        return {"session_id": session_id, "status": "running"}


def add_task(repository=ALLOWED, **overrides):
    values = {
        "repository": repository,
        "repository_url": f"https://github.com/{repository}",
        "issue_number": 3,
        "issue_title": "Bug",
        "droid_session_id": "sess-1",
        "status": TaskStatus.RUNNING,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    with db_session() as session:
        task = Task(**values)
        session.add(task)
        session.flush()
        return task.id


def outcome(text="Done.\nBRANCH: droid/fix-branch"):
    return {"success": True, "subtype": "success", "text": text, "duration_seconds": 5.0, "error": None}


class TestDecisions:
    def test_github_writes_disabled_by_default(self):
        decision = outward_decision(OutwardAction.ISSUE_COMMENT, ALLOWED)
        assert not decision.allowed
        assert "FORGE_ALLOW_GITHUB_WRITES" in decision.reason

    @pytest.mark.parametrize(
        "action", [OutwardAction.PULL_REQUEST_MERGE, OutwardAction.AUTO_MERGE_ENABLE, OutwardAction.WEBHOOK_INSTALL]
    )
    def test_never_permitted_actions_stay_denied_with_writes_enabled(self, monkeypatch, action):
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="true", FORGE_ALLOWED_REPOSITORIES=ALLOWED)
        assert not outward_decision(action, ALLOWED).allowed

    def test_repository_outside_allowlist_is_denied(self, monkeypatch):
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="true", FORGE_ALLOWED_REPOSITORIES=ALLOWED)
        assert outward_decision(OutwardAction.ISSUE_COMMENT, ALLOWED).allowed
        assert outward_decision(OutwardAction.ISSUE_COMMENT, "JAN21DEEPAK/Allowed").allowed
        assert not outward_decision(OutwardAction.ISSUE_COMMENT, OTHER).allowed
        assert not repository_decision("").allowed

    def test_empty_allowlist_denies_everything_in_protected_mode(self, monkeypatch, protected_mode):
        assert not repository_decision(ALLOWED).allowed
        assert not ref_decision("droid/fix").allowed

    def test_ref_scoped_actions_require_allowed_branch(self, monkeypatch):
        configure(
            monkeypatch,
            FORGE_ALLOW_GITHUB_WRITES="true",
            FORGE_ALLOWED_REPOSITORIES=ALLOWED,
            FORGE_ALLOWED_BRANCHES="droid/fix-1,droid/fix-2",
        )
        for ref in ("droid/fix-1", "refs/heads/droid/fix-2", "jan21deepak:droid/fix-1"):
            assert outward_decision(OutwardAction.BRANCH_PUSH, ALLOWED, ref=ref).allowed, ref
        assert not outward_decision(OutwardAction.BRANCH_PUSH, ALLOWED, ref="main").allowed
        assert not outward_decision(OutwardAction.PULL_REQUEST_CREATE, ALLOWED, ref="").allowed
        # Ref policy does not apply to non-ref actions.
        assert outward_decision(OutwardAction.ISSUE_COMMENT, ALLOWED, ref="main").allowed

    def test_changed_configuration_applies_immediately(self, monkeypatch):
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="true", FORGE_ALLOWED_REPOSITORIES=ALLOWED)
        assert outward_decision(OutwardAction.ISSUE_COMMENT, ALLOWED).allowed
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="false")
        assert not outward_decision(OutwardAction.ISSUE_COMMENT, ALLOWED).allowed


class TestGitHubClientGate:
    @pytest.mark.asyncio
    async def test_every_write_is_refused_before_http_by_default(self, outbound, allowlist):
        github = GitHubClient()
        assert await github.post_issue_comment(ALLOWED, 1, "hi") is False
        assert await github.add_issue_label(ALLOWED, 1, "x") is False
        assert await github.ensure_label(ALLOWED, "x") is False
        assert await github.enable_auto_merge(ALLOWED, 1) is False
        with pytest.raises(OutwardActionDeniedError):
            await github.create_issue(ALLOWED, "t")
        with pytest.raises(OutwardActionDeniedError):
            await github.create_pull_request(ALLOWED, title="t", head="droid/x", base="main")
        with pytest.raises(OutwardActionDeniedError):
            await github.create_pull_request_review(ALLOWED, 1, "body")
        with pytest.raises(OutwardActionDeniedError):
            await github.merge_pull_request(ALLOWED, 1)
        with pytest.raises(OutwardActionDeniedError):
            await github.ensure_issues_webhook(ALLOWED, webhook_url="https://x", secret="s" * 20)
        assert outbound == []

    @pytest.mark.asyncio
    async def test_pull_request_from_unapproved_branch_is_refused(self, monkeypatch, outbound, allowlist):
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="true", FORGE_ALLOWED_BRANCHES="droid/ok")
        with pytest.raises(OutwardActionDeniedError):
            await GitHubClient().create_pull_request(ALLOWED, title="t", head="main", base="droid/ok")
        with pytest.raises(OutwardActionDeniedError):
            await GitHubClient().merge_pull_request(ALLOWED, 1)
        assert outbound == []


class TestRegistrationAndRoutes:
    def test_unapproved_repository_registration_makes_no_github_call(self, client, outbound, allowlist):
        resp = client.post("/api/repositories", json={"url": f"https://github.com/{OTHER}"})
        assert resp.status_code == 403
        assert outbound == []
        with db_session() as session:
            assert session.query(Repository).count() == 0

    def test_approved_registration_skips_label_and_webhook_writes(self, client, monkeypatch, outbound, allowlist):
        async def meta(self, full_name):
            return {"full_name": full_name, "html_url": f"https://github.com/{full_name}", "default_branch": "main"}

        monkeypatch.setattr(GitHubClient, "get_repository", meta)
        configure(monkeypatch, PUBLIC_BASE_URL="https://forge.example.test")
        resp = client.post("/api/repositories", json={"url": ALLOWED, "setup_command": "make deps"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["repository"]["setup_command"] == "make deps"
        assert body["repository"]["setup_command_execution"] == "disabled_by_policy"
        assert outbound == []

    def test_renamed_canonical_repository_must_also_be_approved(self, client, monkeypatch, outbound, allowlist):
        async def meta(self, full_name):
            return {"full_name": OTHER, "html_url": f"https://github.com/{OTHER}"}

        monkeypatch.setattr(GitHubClient, "get_repository", meta)
        resp = client.post("/api/repositories", json={"url": ALLOWED})
        assert resp.status_code == 403
        with db_session() as session:
            assert session.query(Repository).count() == 0

    def test_sync_issues_for_unapproved_stored_repository_is_refused(self, client, outbound, allowlist):
        with db_session() as session:
            repo = Repository(full_name=OTHER, url=f"https://github.com/{OTHER}")
            session.add(repo)
            session.flush()
            repo_id = repo.id
        assert client.post(f"/api/repositories/{repo_id}/sync-issues").status_code == 403
        assert client.post(f"/api/repositories/{repo_id}/issues").status_code == 403
        assert outbound == []

    def test_issue_creation_requires_github_writes(self, client, outbound, allowlist):
        with db_session() as session:
            repo = Repository(full_name=ALLOWED, url=f"https://github.com/{ALLOWED}")
            session.add(repo)
            session.flush()
            repo_id = repo.id
        resp = client.post(f"/api/repositories/{repo_id}/issues")
        assert resp.status_code == 403
        assert "FORGE_ALLOW_GITHUB_WRITES" in resp.json()["detail"]
        assert outbound == []

    def test_webhook_install_is_never_permitted(self, client, monkeypatch, outbound, allowlist):
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="true", PUBLIC_BASE_URL="https://forge.example.test")
        assert client.post("/api/webhooks/sync").status_code == 403
        assert outbound == []

    def test_assign_issue_policy(self, client, monkeypatch, outbound, allowlist):
        dispatched = []

        async def fake_dispatch(**kwargs):
            dispatched.append(kwargs)
            return {"task_id": 1, "session_id": None, "status": "queued"}

        monkeypatch.setattr("app.app.create_and_dispatch_task", fake_dispatch)
        with db_session() as session:
            denied = SyncedIssue(repository=OTHER, repository_url="", issue_number=1, title="x")
            allowed = SyncedIssue(repository=ALLOWED, repository_url="", issue_number=2, title="y")
            session.add_all([denied, allowed])
            session.flush()
            ids = [denied.id, allowed.id]

        resp = client.post("/api/issues/assign", json={"issue_ids": ids})
        assert resp.status_code == 200
        results = {r["issue_id"]: r for r in resp.json()["results"]}
        assert results[ids[0]]["ok"] is False
        assert "policy" in results[ids[0]]["detail"]
        # Allowed repository is dispatched locally but not labeled on GitHub.
        assert results[ids[1]]["ok"] is True
        assert results[ids[1]]["label"] is None
        assert "FORGE_ALLOW_GITHUB_WRITES" in results[ids[1]]["label_skipped"]
        assert [d["repository"] for d in dispatched] == [ALLOWED]
        assert outbound == []

    def test_follow_up_on_unapproved_repository_is_refused(self, client, monkeypatch, outbound, allowlist):
        droid = RecordingDroid()
        monkeypatch.setattr("app.app.get_droid_client", lambda: droid)
        denied_id = add_task(repository=OTHER)
        allowed_id = add_task(repository=ALLOWED, issue_number=4, droid_session_id="sess-2")

        resp = client.post(f"/api/tasks/{denied_id}/follow-up", json={"instruction": "more"})
        assert resp.status_code == 403
        assert droid.follow_ups == []

        resp = client.post(f"/api/tasks/{allowed_id}/follow-up", json={"instruction": "more"})
        assert resp.status_code == 200, resp.text
        assert droid.follow_ups == [("sess-2", "more", task_workspace_dir(ALLOWED, allowed_id))]
        assert outbound == []


class TestBackgroundPaths:
    @pytest.mark.asyncio
    async def test_create_task_refuses_unapproved_repository(self, allowlist):
        with pytest.raises(TaskCreateError) as exc:
            await create_and_dispatch_task(
                repository=OTHER, repository_url="", issue_number=1, issue_title="x", issue_body=""
            )
        assert exc.value.status_code == 403
        with db_session() as session:
            assert session.query(Task).count() == 0

    @pytest.mark.asyncio
    async def test_launch_of_old_stored_task_rechecks_policy(self, monkeypatch, allowlist):
        droid = RecordingDroid()
        monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
        task_id = add_task(repository=OTHER, status=TaskStatus.QUEUED, droid_session_id=None)
        await worker.launch_fix_task(
            task_id, repository=OTHER, repository_url="", issue_number=3, issue_title="x", issue_body=""
        )
        assert droid.started == []
        with db_session() as session:
            task = session.get(Task, task_id)
            assert task.status == TaskStatus.FAILED
            assert "repository policy" in task.error

    @pytest.mark.asyncio
    async def test_recovery_skips_unapproved_and_resumes_approved_in_workspace(self, monkeypatch, allowlist):
        droid = RecordingDroid()
        denied_id = add_task(repository=OTHER, droid_session_id="sess-denied")
        allowed_id = add_task(repository=ALLOWED, issue_number=9, droid_session_id="sess-ok")
        with db_session() as session:
            review = ReviewTask(repository=OTHER, pr_number=4, droid_session_id="sess-r", status=TaskStatus.RUNNING)
            session.add(review)
            session.flush()
            review_id = review.id

        recovered = await worker.recover_interrupted_tasks(droid)

        assert recovered == 1
        assert droid.resumed == [("sess-ok", task_workspace_dir(ALLOWED, allowed_id))]
        with db_session() as session:
            assert session.get(Task, denied_id).status == TaskStatus.FAILED
            assert session.get(ReviewTask, review_id).status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_recovery_without_droid_still_refuses_unapproved_rows(self, allowlist):
        droid = RecordingDroid()
        droid.configured = False
        denied_id = add_task(repository=OTHER, droid_session_id="sess-denied")
        allowed_id = add_task(repository=ALLOWED, issue_number=9, droid_session_id="sess-ok")

        assert await worker.recover_interrupted_tasks(droid) == 0

        assert droid.resumed == []
        with db_session() as session:
            denied = session.get(Task, denied_id)
            assert denied.status == TaskStatus.FAILED
            assert "repository policy" in denied.error
            assert session.get(Task, allowed_id).status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_polling_never_reads_unapproved_repositories(self, allowlist):
        github = RecordingGitHub()
        add_task(repository=OTHER, pull_request_url=f"https://github.com/{OTHER}/pull/3", status=TaskStatus.COMPLETED)
        with db_session() as session:
            session.add(ReviewTask(repository=OTHER, pr_number=3, status=TaskStatus.COMPLETED, merged=False))
        await worker.poll_github_state(github)
        assert [c for c in github.calls if OTHER in str(c[1])] == []

    @pytest.mark.asyncio
    async def test_review_of_unapproved_repository_is_not_started(self, monkeypatch, allowlist):
        droid = RecordingDroid()
        monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
        result = await worker.start_pr_review(repository=OTHER, pr_number=1)
        assert result["detail"] == "repository_not_allowed"
        assert droid.started == []
        with db_session() as session:
            assert session.query(ReviewTask).count() == 0

    @pytest.mark.asyncio
    async def test_finalize_without_write_permission_publishes_nothing(self, monkeypatch, allowlist):
        github = RecordingGitHub()
        droid = RecordingDroid()
        monkeypatch.setattr(worker, "GitHubClient", lambda: github)
        monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
        task_id = add_task()

        await worker.finalize_fix_task(task_id, outcome())

        assert droid.pushed == []
        assert github.calls == []
        with db_session() as session:
            task = session.get(Task, task_id)
            assert task.status == TaskStatus.COMPLETED
            assert task.pull_request_url is None

    @pytest.mark.asyncio
    async def test_finalize_refuses_branch_outside_allowlist(self, monkeypatch, allowlist):
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="true", FORGE_ALLOWED_BRANCHES="droid/approved")
        github = RecordingGitHub()
        droid = RecordingDroid()
        monkeypatch.setattr(worker, "GitHubClient", lambda: github)
        monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
        task_id = add_task()

        await worker.finalize_fix_task(task_id, outcome())

        assert droid.pushed == []
        assert [c[0] for c in github.calls] == ["post_issue_comment"]
        assert github.calls[0][1][0] == ALLOWED

    @pytest.mark.asyncio
    async def test_finalize_publishes_follow_up_commits_on_existing_pull_request(self, monkeypatch, allowlist):
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="true", FORGE_ALLOWED_BRANCHES="droid/fix-branch")
        github = RecordingGitHub()
        droid = RecordingDroid()
        monkeypatch.setattr(worker, "GitHubClient", lambda: github)
        monkeypatch.setattr(worker, "get_droid_client", lambda: droid)

        async def no_review(**kwargs):
            return {"review_id": None, "started": False, "detail": "skipped"}

        monkeypatch.setattr(worker, "start_pr_review", no_review)
        task_id = add_task(pull_request_url=f"https://github.com/{ALLOWED}/pull/7")

        await worker.finalize_fix_task(task_id, outcome())

        assert droid.pushed == ["droid/fix-branch"]
        assert "create_pull_request" not in [c[0] for c in github.calls]

    @pytest.mark.asyncio
    async def test_review_verdict_is_not_posted_without_write_permission(self, monkeypatch, allowlist):
        github = RecordingGitHub()
        monkeypatch.setattr(worker, "GitHubClient", lambda: github)
        with db_session() as session:
            review = ReviewTask(repository=ALLOWED, pr_number=4, droid_session_id="s", status=TaskStatus.RUNNING)
            session.add(review)
            session.flush()
            review_id = review.id

        await worker.finalize_review_task(review_id, outcome("Looks fine.\nVERDICT: APPROVE"))

        assert [c[0] for c in github.calls if c[0].startswith(("create_", "post_", "merge", "enable"))] == []


class TestBranchPush:
    @pytest.mark.asyncio
    async def test_denied_push_never_runs_git(self, git_calls, allowlist):
        assert await DroidClient(api_key="fk").ensure_branch_pushed(ALLOWED, 1, "droid/x") is False
        assert git_calls == []

    @pytest.mark.asyncio
    async def test_allowed_push_targets_canonical_credential_free_url(self, monkeypatch, git_calls, allowlist):
        configure(monkeypatch, FORGE_ALLOW_GITHUB_WRITES="true", FORGE_ALLOWED_BRANCHES="droid/x")
        assert await DroidClient(api_key="fk").ensure_branch_pushed(ALLOWED, 1, "droid/x") is True
        canonical = f"https://github.com/{ALLOWED}.git"
        assert ["remote", "set-url", "origin", canonical] in git_calls
        assert ["push", canonical, "HEAD:refs/heads/droid/x"] in git_calls
        token = get_settings().github_token
        assert all(token not in arg for call in git_calls for arg in call)
