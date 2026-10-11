"""End-to-end integration test: GitHub webhook to a recorded local candidate.

Agent success yields a candidate commit only. The pipeline must not push, open
a pull request, start a review or merge on its own; publication is a separate
operator-approved step.

Unlike the unit tests (which exercise one layer at a time), this drives the
real FastAPI app over the ASGI interface against the real SQLite database,
stubbing only the two true external systems: the Droid SDK and the GitHub
REST API. The test's event loop also runs the app's background tasks, so the
webhook-to-PR pipeline executes exactly as it does under uvicorn.
"""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import tasks, worker
from app.candidates import CandidateStatus, WorkspaceState
from app.config import get_settings
from app.database import db_session
from app.models import Repository, Task, TaskStatus

SECRET = "test-secret"
TRIGGER_LABEL = "Droid-complete"
BASE_SHA = "a" * 40
CANDIDATE_SHA = "c" * 40


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def issue_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {
            "full_name": "jan21deepak/superset",
            "html_url": "https://github.com/jan21deepak/superset",
        },
        "issue": {
            "number": 7,
            "title": "Fix the chart legend",
            "body": "The legend overlaps the chart.",
            "labels": [{"name": TRIGGER_LABEL}],
        },
    }


class IntegrationDroid:
    """DroidClient stand-in that completes the fix turn successfully."""

    configured = True
    model = "auto"

    def __init__(self) -> None:
        self.fix_runs: list[dict[str, Any]] = []
        self.pushed_branches: list[str] = []

    def track(self, key: str, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        return task

    async def prepare_fix_workspace(self, **kwargs: Any) -> Path:
        return Path("/tmp/integration-workspace")

    async def ensure_branch_pushed(self, repository: str, task_id: int, branch: str) -> bool:
        self.pushed_branches.append(branch)
        return True

    async def inspect_workspace(self, repository: str, task_id: int, reported_branch: str | None) -> WorkspaceState:
        return WorkspaceState(head_sha=CANDIDATE_SHA, branch=reported_branch, dirty=False)

    async def start_fix_run(self, **kwargs: Any) -> dict[str, Any]:
        self.fix_runs.append(kwargs)
        task_id = kwargs["task_id"]
        with db_session() as session:
            task = session.get(Task, task_id)
            assert task is not None
            task.status = TaskStatus.RUNNING
            task.droid_session_id = "sess-integration-1"
        on_complete = kwargs["on_complete"]

        async def complete() -> None:
            await asyncio.sleep(0.05)
            await on_complete(
                {
                    "success": True,
                    "text": "Fixed the legend.\nBRANCH: droid/fix-legend-7",
                    "duration_seconds": 42.0,
                    "credits": 3,
                    "tokens": 1234,
                }
            )

        asyncio.get_running_loop().create_task(complete())
        return {"session_id": "sess-integration-1", "status": "running", "base_sha": BASE_SHA}

    @property
    def active_sessions(self) -> set[str]:
        return set()


class IntegrationGitHub:
    """GitHubClient stand-in recording comment posts."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    async def post_issue_comment(self, repository: str, issue_number: int, body: str) -> bool:
        self.comments.append((repository, issue_number, body))
        return True


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Wire the app to the stubbed externals and record their interactions."""
    droid = IntegrationDroid()
    github = IntegrationGitHub()
    started_reviews: list[dict[str, Any]] = []

    def record_review(**kwargs: Any) -> dict[str, Any]:
        started_reviews.append(kwargs)
        return {"status": "queued"}

    async def fake_start_pr_review(**kwargs: Any) -> dict[str, Any]:
        return record_review(**kwargs)

    # Intake only accepts signed events for registered, policy-approved repositories.
    monkeypatch.setenv("FORGE_ACCESS_MODE", "protected")
    monkeypatch.setenv("FORGE_OPERATOR_TOKEN", "integration-operator-token-0123456789abcdef")
    monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", "jan21deepak/superset")
    # Writes are enabled to prove agent completion still publishes nothing by itself.
    monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "true")
    monkeypatch.setenv("FORGE_ALLOWED_BRANCHES", "droid/fix-legend-7")
    get_settings.cache_clear()
    with db_session() as session:
        session.add(Repository(full_name="jan21deepak/superset", url="https://github.com/jan21deepak/superset"))

    monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
    # tasks.py imports get_droid_client directly and short-circuits dispatch
    # when unconfigured, so it needs the stub too.
    monkeypatch.setattr(tasks, "get_droid_client", lambda: droid)
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)
    monkeypatch.setattr(worker, "start_pr_review", fake_start_pr_review)
    return {"droid": droid, "github": github, "reviews": started_reviews}


async def _post_webhook(ac: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "issues",
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sign(body),
    }
    return await ac.post("/webhook", content=body, headers=headers)


async def _wait_for_status(task_id: int, wanted: str, timeout: float = 10.0) -> Task:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with db_session() as session:
            task = session.get(Task, task_id)
            if task is not None and task.status == wanted:
                session.expunge(task)
                return task
            current, error = (task.status, task.error) if task is not None else ("<missing>", None)
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"task {task_id} did not reach {wanted} within {timeout}s (stuck at {current}, error={error})"
            )
        await asyncio.sleep(0.1)


class TestIssueToPullRequestFlow:
    async def test_webhook_runs_droid_and_records_unpublished_candidate(self, flow: dict[str, Any]) -> None:
        from app.app import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await _post_webhook(ac, issue_payload())
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        task = await _wait_for_status(task_id, TaskStatus.COMPLETED)

        # The fix turn ran with the issue context and reported the branch.
        assert len(flow["droid"].fix_runs) == 1
        assert flow["droid"].fix_runs[0]["issue_number"] == 7

        # Forge persisted the Droid result and the local candidate it produced.
        assert (task.base_sha, task.candidate_sha) == (BASE_SHA, CANDIDATE_SHA)
        assert task.candidate_branch == "droid/fix-legend-7"
        assert task.candidate_status == CandidateStatus.READY
        assert task.summary is not None and "Fixed the legend." in task.summary
        assert task.duration_seconds == 42.0
        assert task.factory_credits == 3
        assert task.estimated_tokens == 1234

        # A progress comment landed on the issue, stating nothing was published.
        assert flow["github"].comments, "no issue comment was posted"
        repo, issue_number, comment_body = flow["github"].comments[0]
        assert (repo, issue_number) == ("jan21deepak/superset", 7)
        assert "Fixed the legend." in comment_body
        assert "Nothing has been pushed" in comment_body

        # Completion alone neither pushes, opens a PR nor starts a review.
        assert flow["droid"].pushed_branches == []
        assert task.pull_request_url is None
        assert flow["reviews"] == []

    async def test_duplicate_webhook_does_not_recreate_task(self, flow: dict[str, Any]) -> None:
        from app.app import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            first = await _post_webhook(ac, issue_payload())
            second = await _post_webhook(ac, issue_payload())
        assert first.status_code == 202
        assert second.status_code == 200
        assert "duplicate" in second.json()["detail"]
