"""Follow-up and recovery continue in an existing workspace without its legacy credentials.

Workspaces created by earlier versions can hold a token in ``.git/config``
(remote URLs, auth headers, rewrites, helpers). Before any SDK session resumes
there, that configuration is cleaned using the repository the task belongs
to. Local work (branch, commits, dirty and untracked files) is untouched and
no setup, clone, fetch or clean runs. All secrets are fake sentinels.
"""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import droid_client
from app.config import get_settings
from app.database import db_session
from app.droid_client import DroidClient, DroidRunError, task_workspace_dir
from app.models import Task, TaskStatus
from app.redaction import git_basic_auth_value
from app.workspace_config import strip_credential_sections, usable_identity

REPO = "jan21deepak/allowed"
CANONICAL = f"https://github.com/{REPO}.git"
GITHUB_SENTINEL = "fake-gh-legacy-sentinel-Vb73Qa"
FORBIDDEN_GIT = {"clone", "fetch", "pull", "push", "checkout", "reset", "clean", "stash", "restore", "switch"}


def git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True, text=True).stdout.strip()


def make_legacy_workspace(ws: Path) -> dict[str, str]:
    """A task workspace with agent work in progress and token-bearing legacy configuration."""
    ws.mkdir(parents=True)
    git(ws, "init", "-q", "-b", "main")
    (ws / "README.md").write_text("hello\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "init")
    git(ws, "checkout", "-q", "-b", "droid/fix-11")
    (ws / "fix.txt").write_text("committed fix\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "fix")
    (ws / "README.md").write_text("hello\nuncommitted edit\n")
    (ws / "notes-untracked.txt").write_text("agent scratch\n")

    token_url = f"https://x-access-token:{GITHUB_SENTINEL}@github.com/{REPO}.git"
    encoded = git_basic_auth_value(GITHUB_SENTINEL)
    git(ws, "remote", "add", "origin", token_url)
    git(ws, "config", "--add", "remote.origin.pushurl", token_url)
    git(ws, "remote", "add", "fork", f"https://someone:{GITHUB_SENTINEL}@example.com/fork.git")
    git(ws, "config", "http.https://github.com/.extraheader", f"AUTHORIZATION: basic {encoded}")
    git(ws, "config", "http.extraheader", f"AUTHORIZATION: bearer {GITHUB_SENTINEL}")
    git(ws, "config", f"url.https://x-access-token:{GITHUB_SENTINEL}@github.com/.insteadOf", "https://github.com/")
    git(ws, "config", "credential.helper", f"!f() {{ echo password={GITHUB_SENTINEL}; }}; f")
    git(ws, "config", "forge.note", "keep-me")
    return {
        "head": git(ws, "rev-parse", "HEAD"),
        "branch": git(ws, "rev-parse", "--abbrev-ref", "HEAD"),
        "status": git(ws, "status", "--porcelain"),
        "main": git(ws, "rev-parse", "main"),
    }


@pytest.fixture
def secret_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_SENTINEL)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    get_settings.cache_clear()


@pytest.fixture
def recorded_git(monkeypatch):
    calls: list[tuple[str, ...]] = []
    real = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        calls.append(tuple(str(a) for a in args))
        return await real(*args, **kwargs)

    monkeypatch.setattr(droid_client.asyncio, "create_subprocess_exec", recording)
    return calls


class OpenCapturedError(Exception):
    pass


@pytest.fixture
def captured_open(monkeypatch):
    """Snapshot the workspace at the moment the SDK session would open."""
    snapshots: list[dict[str, str]] = []

    async def fake_open(self, *, cwd, **kwargs):
        snapshots.append(
            {
                "config": (Path(cwd) / ".git" / "config").read_text(),
                "listed": git(Path(cwd), "config", "--list", "--show-origin"),
                "kwargs": repr(kwargs),
            }
        )
        raise OpenCapturedError

    monkeypatch.setattr(DroidClient, "_open_session", fake_open)
    return snapshots


async def continue_session(kind: str, ws: Path) -> None:
    client = DroidClient(api_key="fk")
    if kind == "follow_up":
        await client.send_follow_up("sess-legacy", "add a test", repository=REPO, cwd=ws)
    else:
        await client.resume_interrupted("sess-legacy", repository=REPO, cwd=ws)


def assert_no_credentials(text: str) -> None:
    assert GITHUB_SENTINEL not in text
    assert git_basic_auth_value(GITHUB_SENTINEL) not in text
    assert "extraheader" not in text.lower()
    assert "insteadof" not in text.lower()
    assert "credential" not in text.lower()


@pytest.mark.parametrize("kind", ["follow_up", "recovery"])
@pytest.mark.asyncio
async def test_legacy_credentials_removed_before_sdk_opens(kind, secret_env, recorded_git, captured_open):
    ws = task_workspace_dir(REPO, 11)
    before = make_legacy_workspace(ws)

    with pytest.raises(OpenCapturedError):
        await continue_session(kind, ws)

    assert len(captured_open) == 1
    snapshot = captured_open[0]
    assert_no_credentials(snapshot["config"])
    assert_no_credentials(snapshot["listed"])
    assert "resume_id" in snapshot["kwargs"]
    assert git(ws, "remote", "get-url", "origin") == CANONICAL
    assert git(ws, "config", "--get-all", "remote.origin.pushurl") == CANONICAL
    assert git(ws, "remote", "get-url", "fork") == "https://example.com/fork.git"
    assert git(ws, "config", "--get", "forge.note") == "keep-me"
    # Local work survives exactly as the agent left it.
    assert git(ws, "rev-parse", "HEAD") == before["head"]
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD") == before["branch"]
    assert git(ws, "rev-parse", "main") == before["main"]
    assert git(ws, "status", "--porcelain") == before["status"]
    assert (ws / "notes-untracked.txt").read_text() == "agent scratch\n"
    assert (ws / "README.md").read_text() == "hello\nuncommitted edit\n"
    # Only local config inspection/edits ran: no setup shell, clone, fetch or cleanup.
    assert recorded_git, "sanitization ran app-owned git"
    for argv in recorded_git:
        assert argv[0] == "git", argv
        assert not FORBIDDEN_GIT.intersection(argv), argv
        assert not any(GITHUB_SENTINEL in arg for arg in argv), "credentials never reach a command line"


@pytest.mark.parametrize("kind", ["follow_up", "recovery"])
@pytest.mark.asyncio
async def test_clean_workspace_is_left_as_is(kind, secret_env, captured_open):
    ws = task_workspace_dir(REPO, 12)
    ws.mkdir(parents=True)
    git(ws, "init", "-q", "-b", "main")
    git(ws, "remote", "add", "origin", CANONICAL)
    before = (ws / ".git" / "config").read_text()

    with pytest.raises(OpenCapturedError):
        await continue_session(kind, ws)
    assert (ws / ".git" / "config").read_text() == before


@pytest.mark.parametrize("kind", ["follow_up", "recovery"])
@pytest.mark.asyncio
async def test_missing_workspace_refuses_without_opening_or_cloning(kind, secret_env, recorded_git, captured_open):
    ws = task_workspace_dir(REPO, 13)
    with pytest.raises(DroidRunError, match="workspace"):
        await continue_session(kind, ws)
    assert captured_open == []
    assert not ws.exists()
    assert not any("clone" in argv for argv in recorded_git)


@pytest.mark.parametrize("kind", ["follow_up", "recovery"])
@pytest.mark.asyncio
async def test_directory_inside_another_repository_is_not_rewritten(kind, secret_env, captured_open, tmp_path):
    """A workspace without its own .git must never edit an enclosing repository's config."""
    outer = tmp_path / "ws"
    outer.mkdir(parents=True)
    git(outer, "init", "-q", "-b", "main")
    git(outer, "remote", "add", "origin", f"https://x-access-token:{GITHUB_SENTINEL}@github.com/other/repo.git")
    outer_config = (outer / ".git" / "config").read_text()
    ws = task_workspace_dir(REPO, 14)
    ws.mkdir(parents=True)

    with pytest.raises(DroidRunError, match="workspace"):
        await continue_session(kind, ws)
    assert captured_open == []
    assert (outer / ".git" / "config").read_text() == outer_config


@pytest.mark.parametrize("kind", ["follow_up", "recovery"])
@pytest.mark.asyncio
async def test_continuation_requires_usable_git_identity(kind, secret_env, captured_open, monkeypatch, tmp_path):
    ws = task_workspace_dir(REPO, 15)
    make_legacy_workspace(ws)
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))

    with pytest.raises(DroidRunError, match="Git identity"):
        await continue_session(kind, ws)
    assert captured_open == []
    assert "[user]" not in (ws / ".git" / "config").read_text()
    assert empty.read_text() == ""


@pytest.mark.parametrize(
    ("ident", "usable"),
    [
        ("Operator <op@example.com> 1700000000 +0800", True),
        ("Operator <> 1700000000 +0800", False),
        ("   <op@example.com> 1700000000 +0800", False),
        ("garbage", False),
    ],
)
def test_usable_identity(ident, usable):
    assert usable_identity(ident) is usable


def test_unrelated_sections_survive_credential_section_stripping():
    text = (
        '[core]\n\tbare = false\n[url "https://u:secret-value@github.com/"]\n\tinsteadOf = https://github.com/\n'
        '[branch "main"]\n\tremote = origin\n'
    )
    assert strip_credential_sections(text) == '[core]\n\tbare = false\n[branch "main"]\n\tremote = origin\n'


def test_follow_up_route_sanitizes_before_resume(client, secret_env, monkeypatch):
    """Actual follow-up API: the stored task's repository drives the cleanup."""
    monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", REPO)
    get_settings.cache_clear()
    with db_session() as session:
        task = Task(repository=REPO, issue_number=3, status=TaskStatus.COMPLETED, droid_session_id="sess-http")
        session.add(task)
        session.flush()
        task_id = task.id
    ws = task_workspace_dir(REPO, task_id)
    before = make_legacy_workspace(ws)
    opened: list[str] = []

    async def fake_open(self, *, cwd, resume_id=None, **kwargs):
        opened.append((Path(cwd) / ".git" / "config").read_text())
        return SimpleNamespace(id=resume_id)

    monkeypatch.setattr(DroidClient, "_open_session", fake_open)
    monkeypatch.setattr(DroidClient, "_spawn_turn", lambda self, session, prompt, **kw: session.id)
    monkeypatch.setattr(DroidClient, "configured", property(lambda self: True))

    resp = client.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "add a regression test"})

    assert resp.status_code == 200, resp.text
    assert len(opened) == 1
    assert_no_credentials(opened[0])
    assert GITHUB_SENTINEL not in resp.text
    assert git(ws, "rev-parse", "HEAD") == before["head"]
    assert git(ws, "status", "--porcelain") == before["status"]
