"""Labeled local Git/GitHub fixtures for delivery tests.

``LocalRemote`` is a bare repository standing in for the canonical GitHub
remote (``droid_client._remote_url`` is redirected to it), so pushes and
``ls-remote`` really run, but only against a temporary directory.
``FakeDeliveryGitHub`` answers the read and PR calls publication makes,
deriving a PR's head from the local remote the way GitHub derives it from the
pushed branch. Nothing here reaches GitHub.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app import droid_client
from app.candidates import WorkspaceState, record_candidate
from app.check_config import configured_checks
from app.config import get_settings
from app.database import db_session
from app.delivery_models import CheckResult, CheckRun, CheckStatus
from app.droid_client import task_workspace_dir
from app.models import ReviewTask, Task, TaskStatus
from app.review_outcomes import REVIEW_TARGET_CANDIDATE
from app.review_policy import REVIEW_POLICY_RESTRICTED, ReviewVerdict

REPO = "jan21deepak/droid_ai_engineer"
BRANCH = "droid_mission"
CHECKS_ENV = '[{"name": "unit", "argv": ["python3", "-c", "print(1)"]}]'


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class LocalRemote:
    """A bare repo with ``main`` and ``droid_mission``, plus a task workspace cloned on ``droid_mission``."""

    def __init__(self, root: Path):
        self.root = root
        self.bare = root / "remote.git"
        seed = root / "seed"
        seed.mkdir(parents=True)
        git("init", "-q", "--bare", "-b", "main", str(self.bare), cwd=root)
        git("init", "-q", "-b", "main", cwd=seed)
        (seed / "README.md").write_text("seed\n")
        git("add", "README.md", cwd=seed)
        git("commit", "-q", "-m", "seed", cwd=seed)
        git("remote", "add", "origin", str(self.bare), cwd=seed)
        git("push", "-q", "origin", "main", f"main:{BRANCH}", cwd=seed)
        self.base_sha = git("rev-parse", "HEAD", cwd=seed)

    def clone_workspace(self, task_id: int) -> Path:
        ws = task_workspace_dir(REPO, task_id)
        ws.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", "--branch", BRANCH, str(self.bare), str(ws), cwd=self.root)
        return ws

    def head(self, branch: str = BRANCH) -> str | None:
        out = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=self.bare,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip() if out.returncode == 0 else None


def commit(ws: Path, name: str, content: str) -> str:
    (ws / name).write_text(content)
    git("add", name, cwd=ws)
    git("commit", "-q", "-m", f"change {name}", cwd=ws)
    return git("rev-parse", "HEAD", cwd=ws)


def configure(monkeypatch, tmp_path: Path, *, writes: bool = True, checks: str = CHECKS_ENV) -> LocalRemote:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", REPO)
    monkeypatch.setenv("FORGE_ALLOWED_BRANCHES", BRANCH)
    monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "true" if writes else "false")
    monkeypatch.setenv("FORGE_ACCEPTANCE_CHECKS", checks)
    get_settings.cache_clear()
    remote = LocalRemote(tmp_path / "git")
    monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(remote.bare))
    return remote


def add_candidate_task(remote: LocalRemote, *, content: str = "fix\n") -> tuple[int, str, Path]:
    """A completed task whose workspace holds one clean commit on ``droid_mission``; returns (id, sha, ws)."""
    with db_session() as session:
        task = Task(
            repository=REPO,
            repository_url=f"https://github.com/{REPO}",
            issue_number=7,
            issue_title="Clarify docs",
            status=TaskStatus.COMPLETED,
            base_sha=remote.base_sha,
            created_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        task_id = task.id
    ws = remote.clone_workspace(task_id)
    sha = commit(ws, "fix.txt", content)
    record_candidate(task_id, WorkspaceState(head_sha=sha, branch=BRANCH, dirty=False))
    return task_id, sha, ws


def recandidate(task_id: int, ws: Path, name: str = "more.txt") -> str:
    """A follow-up turn produced a new candidate B on the same branch."""
    sha = commit(ws, name, "follow-up\n")
    record_candidate(task_id, WorkspaceState(head_sha=sha, branch=BRANCH, dirty=False))
    return sha


def add_check_run(
    task_id: int,
    sha: str,
    status: str = CheckStatus.PASSED,
    *,
    evaluated: str | None = None,
    legacy: bool = False,
) -> int:
    """Labeled fixture: an app check run of the currently configured checks already finished on ``sha``.

    ``legacy`` stores the names-only configuration written before results were
    bound to each check's effective specification.
    """
    specs = configured_checks().checks
    with db_session() as session:
        run = CheckRun(
            task_id=task_id,
            candidate_sha=sha,
            evaluated_sha=evaluated if evaluated is not None else sha,
            status=status,
            configuration=json.dumps([s.name for s in specs] if legacy else [s.identity() for s in specs]),
        )
        session.add(run)
        session.flush()
        result_status = CheckStatus.PASSED if status == CheckStatus.PASSED else CheckStatus.FAILED
        for position, spec in enumerate(specs):
            session.add(
                CheckResult(
                    run_id=run.id,
                    position=position,
                    name=spec.name,
                    command=spec.display_command,
                    status=result_status,
                )
            )
        return run.id


def add_candidate_review(
    task_id: int,
    sha: str,
    base_sha: str,
    *,
    verdict: str | None = ReviewVerdict.APPROVE,
    status: str = TaskStatus.COMPLETED,
    diff_complete: bool = True,
) -> int:
    """Labeled fixture: a restricted candidate review already finished on ``sha``."""
    with db_session() as session:
        review = ReviewTask(
            repository=REPO,
            repository_url=f"https://github.com/{REPO}",
            pr_number=0,
            pr_title="candidate",
            pr_url="",
            status=status,
            review_target=REVIEW_TARGET_CANDIDATE,
            task_id=task_id,
            commit_sha=sha,
            base_sha=base_sha,
            diff_complete=diff_complete,
            review_policy=REVIEW_POLICY_RESTRICTED,
            verdict=verdict,
            findings="[]",
            summary="VERDICT: APPROVE" if verdict == ReviewVerdict.APPROVE else "VERDICT: REQUEST_CHANGES",
        )
        session.add(review)
        session.flush()
        return review.id


def http_error(status: int, method: str = "POST") -> httpx.HTTPStatusError:
    request = httpx.Request(method, "https://api.github.com/fixture")
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=httpx.Response(status, request=request))


class FakeDeliveryGitHub:
    """Fixture GitHub: PR heads follow the local remote's branch tip."""

    def __init__(self, remote: LocalRemote, *, default_branch: str = "main"):
        self.remote = remote
        self.default_branch = default_branch
        self.prs: dict[int, dict] = {}
        self.calls: list[str] = []
        self.create_error: Exception | None = None
        self.repository_error: Exception | None = None
        self.stale_head: str | None = None
        self.check_runs: list[dict] = []
        self.statuses: list[dict] = []
        self.ci_error: Exception | None = None

    def _pr(self, number: int) -> dict:
        pr = dict(self.prs[number])
        pr["head"] = {"ref": pr["head_ref"], "sha": self.stale_head or self.remote.head(pr["head_ref"])}
        return pr

    async def get_repository(self, full_name):
        self.calls.append("get_repository")
        if self.repository_error is not None:
            raise self.repository_error
        return {"default_branch": self.default_branch}

    async def find_open_pull_request_for_head(self, repository, head):
        self.calls.append("find_open_pull_request_for_head")
        for number, pr in self.prs.items():
            if pr["head_ref"] == head and pr["state"] == "open":
                return self._pr(number)
        return None

    async def create_pull_request(self, repository, *, title, head, base, body="", draft=False, same_repo=True):
        self.calls.append("create_pull_request")
        if self.create_error is not None:
            raise self.create_error
        number = 100 + len(self.prs)
        self.prs[number] = {
            "number": number,
            "html_url": f"https://github.com/{repository}/pull/{number}",
            "draft": draft,
            "state": "open",
            "merged": False,
            "head_ref": head,
            "base": {"ref": base},
            "title": title,
            "body": body,
        }
        return self._pr(number)

    async def get_pull_request(self, repository, pr_number):
        self.calls.append("get_pull_request")
        return self._pr(pr_number)

    async def list_commit_check_runs(self, repository, sha):
        self.calls.append("list_commit_check_runs")
        if self.ci_error is not None:
            raise self.ci_error
        return self.check_runs

    async def get_commit_status(self, repository, sha):
        self.calls.append("get_commit_status")
        return {"statuses": self.statuses}

    def writes(self) -> list[str]:
        return [c for c in self.calls if c == "create_pull_request"]
