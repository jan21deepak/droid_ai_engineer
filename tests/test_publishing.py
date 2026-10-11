"""App-owned publication primitives and additive candidate/review schema columns."""

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text

from app import database
from app.config import get_settings
from app.database import db_session
from app.models import ReviewTask, Task, TaskStatus
from app.policy import OutwardActionDeniedError
from app.publishing import ensure_pull_request_for_task

REPO = "jan21deepak/droid_ai_engineer"


class PullRequestGitHub:
    def __init__(self, existing=None, create_error=None):
        self.existing = existing
        self.create_error = create_error
        self.created: list[dict] = []

    async def find_open_pull_request_for_head(self, repository, head):
        return self.existing

    async def get_repository(self, full_name):
        return {"default_branch": "main"}

    async def create_pull_request(self, repository, **kwargs):
        if self.create_error is not None:
            raise self.create_error
        self.created.append(kwargs)
        return {"number": 5, "html_url": f"https://github.com/{repository}/pull/5"}


def add_task(**overrides):
    values = {"repository": REPO, "issue_number": 3, "issue_title": "Docs", "status": TaskStatus.COMPLETED}
    values.update(overrides)
    with db_session() as session:
        task = Task(**values)
        session.add(task)
        session.flush()
        session.expunge(task)
        return task


@pytest.fixture
def writes(monkeypatch):
    monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "true")
    monkeypatch.setenv("FORGE_ALLOWED_BRANCHES", "droid_mission")
    get_settings.cache_clear()


class TestEnsurePullRequest:
    @pytest.mark.asyncio
    async def test_denied_without_write_permission(self):
        github = PullRequestGitHub()
        assert await ensure_pull_request_for_task(add_task(), "droid_mission", github) is None
        assert github.created == []

    @pytest.mark.asyncio
    async def test_denied_for_unapproved_branch(self, writes):
        github = PullRequestGitHub()
        assert await ensure_pull_request_for_task(add_task(), "main", github) is None
        assert github.created == []

    @pytest.mark.asyncio
    async def test_creates_same_repo_pr_and_persists_url(self, writes):
        task = add_task()
        github = PullRequestGitHub()
        url = await ensure_pull_request_for_task(task, "droid_mission", github)
        assert url == f"https://github.com/{REPO}/pull/5"
        [created] = github.created
        assert (created["head"], created["base"], created["same_repo"]) == ("droid_mission", "main", True)
        assert "(#3)" in created["title"]
        with db_session() as session:
            row = session.get(Task, task.id)
            assert (row.pull_request_url, row.pr_state) == (url, "open")

    @pytest.mark.asyncio
    async def test_reuses_open_pr_after_create_race(self, writes):
        request = httpx.Request("POST", "https://api.github.com/x")
        race = httpx.HTTPStatusError("exists", request=request, response=httpx.Response(422, request=request))
        github = PullRequestGitHub(create_error=race)
        lookups: list[str] = []

        async def found_on_second_lookup(repository, head):
            lookups.append(head)
            return {"html_url": f"https://github.com/{REPO}/pull/8"} if len(lookups) > 1 else None

        github.find_open_pull_request_for_head = found_on_second_lookup
        url = await ensure_pull_request_for_task(add_task(), "droid_mission", github)
        assert url == f"https://github.com/{REPO}/pull/8"
        assert len(lookups) == 2

    @pytest.mark.asyncio
    async def test_github_client_itself_refuses_pr_without_policy(self):
        from app.github import GitHubClient

        with pytest.raises(OutwardActionDeniedError):
            await GitHubClient().create_pull_request(REPO, title="t", head="droid_mission", base="main")


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

    columns = {c["name"] for c in inspect(database.get_engine()).get_columns("review_tasks")}
    assert {"base_sha", "diff_complete", "review_policy", "verdict", "findings"} <= columns
    with db_session() as session:
        task = session.get(Task, 1).to_dict()
        review = session.get(ReviewTask, 1).to_dict()
    assert task["candidate"] == {"sha": None, "branch": None, "status": None, "recorded_at": None}
    assert task["base_sha"] is None
    assert (review["verdict"], review["diff_complete"], review["review_policy"], review["findings"]) == (
        None,
        None,
        None,
        None,
    )
