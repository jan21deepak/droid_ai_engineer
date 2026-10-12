"""Explicit app-owned publication of one approved candidate SHA.

Pushes go to a labeled local bare repository standing in for GitHub
(``tests/delivery_fixtures.py``); PR calls go to a fixture GitHub. Nothing
reaches the real remote.
"""

import asyncio

import pytest
from sqlalchemy import create_engine, inspect, text

from app import database, publishing
from app.config import get_settings
from app.database import db_session
from app.delivery import DeliveryRefusedError, approve_candidate, task_delivery
from app.delivery_models import CheckStatus, Publication, PublicationStatus
from app.droid_client import task_workspace_dir
from app.models import ReviewTask, Task
from app.policy import OutwardActionDeniedError
from app.publishing import publish_candidate, recover_interrupted_publications
from app.review_policy import ReviewVerdict
from tests.delivery_fixtures import (
    BRANCH,
    REPO,
    FakeDeliveryGitHub,
    LocalRemote,
    add_candidate_review,
    add_candidate_task,
    add_check_run,
    commit,
    configure,
    git,
    http_error,
    recandidate,
)


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setattr(publishing, "PR_HEAD_RECHECK_DELAYS", (0.0, 0.0))
    return configure(monkeypatch, tmp_path)


@pytest.fixture
def github(remote):
    return FakeDeliveryGitHub(remote)


async def approved_candidate(remote: LocalRemote) -> tuple[int, str]:
    task_id, sha, _ws = add_candidate_task(remote)
    add_check_run(task_id, sha)
    add_candidate_review(task_id, sha, remote.base_sha)
    await approve_candidate(task_id, sha, approved_via="bearer")
    return task_id, sha


def remote_branches(remote: LocalRemote) -> set[str]:
    out = git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=remote.bare)
    return set(out.split())


async def refused(task_id: int, sha: str, github) -> DeliveryRefusedError:
    with pytest.raises(DeliveryRefusedError) as info:
        await publish_candidate(task_id, sha, github=github)
    return info.value


class TestPublishes:
    @pytest.mark.asyncio
    async def test_pushes_exact_sha_to_droid_mission_and_opens_one_draft(self, remote, github):
        task_id, sha = await approved_candidate(remote)

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert result["published"] is True
        assert (pub["status"], pub["pushed"], pub["push_detail"]) == ("published", True, "pushed")
        assert (pub["remote_head_sha"], pub["remote_head_matches"]) == (sha, True)
        assert remote.head(BRANCH) == sha
        assert remote.head("main") == remote.base_sha
        # The mission branch is updated in place; no other branch is created.
        assert remote_branches(remote) == {"main", BRANCH}
        pr = pub["pull_request"]
        assert (pr["draft"], pr["state"], pr["head_sha"], pr["head_matches"], pr["action"]) == (
            True,
            "open",
            sha,
            True,
            "created",
        )
        assert pr["merged"] is False
        [created] = github.prs.values()
        assert (created["head_ref"], created["base"]["ref"], created["draft"]) == (BRANCH, "main", True)
        assert "(#7)" in created["title"]
        assert sha in created["body"]
        assert "not a GitHub review" in created["body"]
        with db_session() as session:
            task = session.get(Task, task_id)
            assert (task.pull_request_url, task.pr_state) == (pr["url"], "open")
        state = task_delivery(task_id)
        assert state["readiness"]["state"] == "published"
        assert state["readiness"]["ready_to_publish"] is False
        assert state["merge"]["enabled"] is False
        # Opening the PR says nothing about CI yet.
        assert state["remote_ci"]["state"] == "not_observed"

    @pytest.mark.asyncio
    async def test_repeat_publish_is_idempotent(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        await publish_candidate(task_id, sha, github=github)
        calls_after_first = list(github.calls)

        again = await publish_candidate(task_id, sha, github=github)

        assert again["published"] is True and again["already_published"] is True
        assert github.calls == calls_after_first
        assert len(github.prs) == 1
        with db_session() as session:
            assert session.query(Publication).count() == 1

    @pytest.mark.asyncio
    async def test_existing_open_pr_for_the_branch_is_reused(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        await github.create_pull_request(REPO, title="earlier", head=BRANCH, base="main", draft=True)
        github.calls.clear()

        result = await publish_candidate(task_id, sha, github=github)

        assert result["published"] is True
        assert result["publication"]["pull_request"]["action"] == "reused"
        assert github.writes() == []
        assert len(github.prs) == 1

    @pytest.mark.asyncio
    async def test_create_race_422_reuses_the_pr_that_appeared(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        original_find = github.find_open_pull_request_for_head
        lookups = []

        async def find(repository, head):
            lookups.append(head)
            if len(lookups) == 1:
                return None
            # Another request opened the PR between our lookup and create.
            error, github.create_error = github.create_error, None
            await github.create_pull_request(repository, title="raced", head=head, base="main", draft=True)
            github.create_error = error
            return await original_find(repository, head)

        github.find_open_pull_request_for_head = find
        github.create_error = http_error(422)

        result = await publish_candidate(task_id, sha, github=github)

        assert result["published"] is True
        assert result["publication"]["pull_request"]["action"] == "reused"
        assert len(lookups) == 2 and len(github.prs) == 1
        assert github.writes() == ["create_pull_request", "create_pull_request"]


class TestRefusedBeforeAnyOutwardEffect:
    @pytest.mark.asyncio
    async def test_unapproved_candidate(self, remote, github):
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)

        error = await refused(task_id, sha, github)

        assert (error.status_code, error.detail) == (409, "not_ready_to_publish")
        assert [b["code"] for b in error.extra["blockers"]] == ["approval_missing"]
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

    @pytest.mark.parametrize(
        ("check_status", "verdict", "codes"),
        [
            (CheckStatus.FAILED, ReviewVerdict.APPROVE, ["checks_failed", "approval_missing"]),
            (None, ReviewVerdict.APPROVE, ["checks_missing", "approval_missing"]),
            (CheckStatus.PASSED, ReviewVerdict.REQUEST_CHANGES, ["review_changes_requested", "approval_missing"]),
            (CheckStatus.PASSED, None, ["review_missing", "approval_missing"]),
        ],
        ids=["failed_checks", "missing_checks", "nonapproving_review", "missing_review"],
    )
    @pytest.mark.asyncio
    async def test_missing_or_failing_evidence(self, remote, github, check_status, verdict, codes):
        task_id, sha, _ws = add_candidate_task(remote)
        if check_status is not None:
            add_check_run(task_id, sha, check_status)
        if verdict is not None:
            add_candidate_review(task_id, sha, remote.base_sha, verdict=verdict)

        error = await refused(task_id, sha, github)

        assert [b["code"] for b in error.extra["blockers"]] == codes
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

    @pytest.mark.asyncio
    async def test_stale_sha_after_follow_up(self, remote, github):
        task_id, sha_a = await approved_candidate(remote)
        with db_session() as session:
            ws_sha = session.get(Task, task_id).candidate_sha
        assert ws_sha == sha_a
        sha_b = recandidate(task_id, task_workspace_dir(REPO, task_id))

        stale = await refused(task_id, sha_a, github)
        assert (stale.status_code, stale.detail) == (409, "stale_candidate")
        assert stale.extra["current_sha"] == sha_b
        # B has no checks, review or approval of its own; A's do not carry over.
        unapproved = await refused(task_id, sha_b, github)
        assert {b["code"] for b in unapproved.extra["blockers"]} == {
            "checks_missing",
            "review_missing",
            "approval_missing",
        }
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

    @pytest.mark.asyncio
    async def test_dirty_workspace(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        (task_workspace_dir(REPO, task_id) / "fix.txt").write_text("uncommitted edit\n")

        error = await refused(task_id, sha, github)

        assert (error.status_code, error.detail) == (409, "dirty_workspace")
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha
        with db_session() as session:
            assert session.query(Publication).count() == 0

    @pytest.mark.asyncio
    async def test_workspace_moved_past_recorded_candidate(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        moved = commit(task_workspace_dir(REPO, task_id), "late.txt", "unrecorded\n")

        error = await refused(task_id, sha, github)

        assert (error.detail, error.extra["workspace_sha"]) == ("stale_candidate", moved)
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

    @pytest.mark.asyncio
    async def test_github_writes_disabled(self, remote, github, monkeypatch):
        task_id, sha = await approved_candidate(remote)
        monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "false")
        get_settings.cache_clear()

        error = await refused(task_id, sha, github)

        assert (error.status_code, error.detail) == (403, "policy_denied")
        assert "FORGE_ALLOW_GITHUB_WRITES" in error.extra["reason"]
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

    @pytest.mark.asyncio
    async def test_branch_not_explicitly_allowed_even_in_local_dev(self, remote, github, monkeypatch):
        task_id, sha = await approved_candidate(remote)
        monkeypatch.setenv("FORGE_ALLOWED_BRANCHES", "")
        get_settings.cache_clear()

        error = await refused(task_id, sha, github)

        assert error.detail == "policy_denied"
        assert "FORGE_ALLOWED_BRANCHES" in error.extra["reason"]
        assert github.calls == [] and remote.head(BRANCH) == remote.base_sha

    @pytest.mark.asyncio
    async def test_default_branch_is_never_a_publication_target(self, remote, monkeypatch):
        github = FakeDeliveryGitHub(remote, default_branch=BRANCH)
        task_id, sha = await approved_candidate(remote)

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert (result["published"], pub["status"], pub["pushed"]) == (False, "failed", False)
        assert "default branch" in pub["error"]
        assert remote.head(BRANCH) == remote.base_sha
        assert github.writes() == []

    @pytest.mark.asyncio
    async def test_concurrent_publish_is_refused(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        lock = publishing._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            error = await refused(task_id, sha, github)
        assert (error.status_code, error.detail) == (409, "publication_in_progress")
        assert github.calls == []

    @pytest.mark.asyncio
    async def test_github_client_itself_refuses_pr_without_policy(self):
        from app.github import GitHubClient

        with pytest.raises(OutwardActionDeniedError):
            await GitHubClient().create_pull_request(REPO, title="t", head=BRANCH, base="main")


class TestPartialFailures:
    @pytest.mark.asyncio
    async def test_rejected_push_is_not_forced_and_opens_no_pr(self, remote, github, tmp_path):
        task_id, sha = await approved_candidate(remote)
        other = tmp_path / "other"
        git("clone", "-q", "--branch", BRANCH, str(remote.bare), str(other), cwd=tmp_path)
        moved = commit(other, "elsewhere.txt", "someone else\n")
        git("push", "-q", "origin", BRANCH, cwd=other)

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert (result["published"], pub["status"], pub["pushed"], pub["push_detail"]) == (
            False,
            "push_failed",
            False,
            "rejected",
        )
        assert "never forced" in pub["error"]
        assert remote.head(BRANCH) == moved
        assert github.writes() == []
        assert task_delivery(task_id)["readiness"]["state"] == "publication_incomplete"
        assert task_delivery(task_id)["remote_ci"]["state"] == "not_published"

    @pytest.mark.asyncio
    async def test_remote_head_mismatch_after_push(self, remote, github, monkeypatch):
        task_id, sha = await approved_candidate(remote)
        reads = iter([remote.base_sha, "f" * 40])

        async def remote_head(repository, branch):
            return next(reads)

        monkeypatch.setattr(publishing, "remote_branch_head", remote_head)

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert (pub["status"], pub["pushed"], pub["remote_head_sha"], pub["remote_head_matches"]) == (
            "remote_mismatch",
            True,
            "f" * 40,
            False,
        )
        assert result["published"] is False
        assert github.writes() == []

    @pytest.mark.asyncio
    async def test_pr_failure_keeps_push_fact_and_retry_does_not_duplicate(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        github.create_error = http_error(500)

        first = await publish_candidate(task_id, sha, github=github)

        pub = first["publication"]
        assert (first["published"], pub["status"], pub["pushed"], pub["remote_head_sha"]) == (
            False,
            "pr_failed",
            True,
            sha,
        )
        assert pub["pull_request"]["number"] is None
        assert remote.head(BRANCH) == sha

        github.create_error = None
        retry = await publish_candidate(task_id, sha, github=github)

        pub = retry["publication"]
        assert (retry["published"], pub["status"], pub["push_detail"], pub["publish_attempts"]) == (
            True,
            "published",
            "already_on_remote",
            2,
        )
        assert len(github.writes()) == 2 and len(github.prs) == 1
        with db_session() as session:
            assert session.query(Publication).count() == 1

    @pytest.mark.asyncio
    async def test_pr_head_that_never_matches_is_reported(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        github.stale_head = "e" * 40

        result = await publish_candidate(task_id, sha, github=github)

        pub = result["publication"]
        assert (result["published"], pub["status"], pub["pushed"]) == (False, "pr_head_mismatch", True)
        assert (pub["pull_request"]["head_sha"], pub["pull_request"]["head_matches"]) == ("e" * 40, False)
        assert pub["pull_request"]["url"]

    @pytest.mark.asyncio
    async def test_reused_pr_head_catches_up_after_recheck(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        await github.create_pull_request(REPO, title="earlier", head=BRANCH, base="main", draft=True)
        github.stale_head = remote.base_sha
        original = github.get_pull_request

        async def caught_up(repository, number):
            github.stale_head = None
            return await original(repository, number)

        github.get_pull_request = caught_up

        result = await publish_candidate(task_id, sha, github=github)

        assert result["published"] is True
        assert result["publication"]["pull_request"]["head_sha"] == sha

    @pytest.mark.asyncio
    async def test_base_branch_lookup_failure_pushes_nothing(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        github.repository_error = http_error(503, "GET")

        result = await publish_candidate(task_id, sha, github=github)

        assert (result["publication"]["status"], result["publication"]["pushed"]) == ("failed", False)
        assert remote.head(BRANCH) == remote.base_sha

    @pytest.mark.asyncio
    async def test_restart_mid_publication_is_retryable(self, remote, github):
        task_id, sha = await approved_candidate(remote)
        github.create_error = http_error(500)
        await publish_candidate(task_id, sha, github=github)
        with db_session() as session:
            session.query(Publication).update({"status": PublicationStatus.PUBLISHING})

        assert recover_interrupted_publications() == 1
        assert task_delivery(task_id)["publication"]["status"] == "interrupted"

        github.create_error = None
        result = await publish_candidate(task_id, sha, github=github)
        assert result["published"] is True and len(github.prs) == 1

    @pytest.mark.asyncio
    async def test_new_candidate_after_publication_needs_its_own_approval(self, remote, github):
        task_id, sha_a = await approved_candidate(remote)
        await publish_candidate(task_id, sha_a, github=github)
        sha_b = recandidate(task_id, task_workspace_dir(REPO, task_id))

        state = task_delivery(task_id)
        assert state["publication"] is None
        assert state["latest_publication"]["candidate_sha"] == sha_a
        assert state["readiness"]["state"] == "not_ready"
        error = await refused(task_id, sha_b, github)
        assert "approval_missing" in {b["code"] for b in error.extra["blockers"]}
        assert remote.head(BRANCH) == sha_a


def test_old_schema_gains_nullable_candidate_and_review_columns(tmp_path, monkeypatch):
    """Rows created before these columns existed read back as unknown, not invented values."""
    url = f"sqlite:///{tmp_path}/old.db"
    old = create_engine(url)
    with old.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, repository VARCHAR(255), repository_url VARCHAR(512),"
                " issue_number INTEGER, issue_title VARCHAR(512), issue_body TEXT, labels TEXT,"
                " droid_session_id VARCHAR(128), status VARCHAR(32), pull_request_url VARCHAR(512),"
                " pr_state VARCHAR(16), pr_merged_at DATETIME, summary TEXT, error TEXT, factory_credits FLOAT,"
                " estimated_tokens INTEGER, cost_usd FLOAT, created_at DATETIME, completed_at DATETIME,"
                " duration_seconds FLOAT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE review_tasks (id INTEGER PRIMARY KEY, repository VARCHAR(255),"
                " repository_url VARCHAR(512), pr_number INTEGER, pr_title VARCHAR(512), pr_url VARCHAR(512),"
                " commit_sha VARCHAR(64), droid_session_id VARCHAR(128), status VARCHAR(32), merged BOOLEAN,"
                " auto_merge_enabled BOOLEAN, summary TEXT, error TEXT, factory_credits FLOAT, cost_usd FLOAT,"
                " created_at DATETIME, completed_at DATETIME, duration_seconds FLOAT)"
            )
        )
        conn.execute(text("INSERT INTO tasks (id, repository, issue_number, status) VALUES (1, 'o/r', 2, 'completed')"))
        conn.execute(
            text(
                "INSERT INTO review_tasks (id, repository, pr_number, status, merged, auto_merge_enabled, summary)"
                " VALUES (1, 'o/r', 3, 'completed', 0, 1, 'VERDICT: APPROVE')"
            )
        )
    old.dispose()

    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    database.reset_for_tests()
    database.init_db()
    database.init_db()  # idempotent

    engine_inspector = inspect(database.get_engine())
    columns = {c["name"] for c in engine_inspector.get_columns("review_tasks")}
    assert {"base_sha", "diff_complete", "review_policy", "verdict", "findings"} <= columns
    assert {"review_target", "task_id", "github_review_outcome", "github_review_event"} <= columns
    assert "candidate_generation" in {c["name"] for c in engine_inspector.get_columns("tasks")}
    assert {"check_runs", "check_results", "delivery_approvals", "publications"} <= set(
        engine_inspector.get_table_names()
    )
    with db_session() as session:
        task = session.get(Task, 1).to_dict()
        review = session.get(ReviewTask, 1).to_dict()
    assert task["candidate"] == {"sha": None, "branch": None, "status": None, "recorded_at": None, "generation": None}
    assert task["base_sha"] is None
    assert (review["verdict"], review["diff_complete"], review["review_policy"], review["findings"]) == (
        None,
        None,
        None,
        None,
    )
    # Legacy rows were PR reviews whose GitHub outcome was never recorded.
    assert review["review_target"] == "pull_request"
    assert review["github_outcome"]["kind"] == "unknown"
    assert review["github_outcome"]["formal_github_approval"] is False
