"""Tests for the droid-sdk backed client."""

import asyncio
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import droid_client
from app.config import get_settings
from app.droid_client import (
    DroidClient,
    DroidRunError,
    DroidSessionBusyError,
    build_fix_prompt,
    build_follow_up_prompt,
    build_review_prompt,
    extract_branch_name,
    extract_verdict,
    map_outcome_status,
    review_workspace_dir,
    task_workspace_dir,
)
from app.models import TaskStatus


def make_result(*, success=True, text="Done", duration=90.0, credits=1.5, subtype="success"):
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_tokens=0, factory_credits=credits
    )
    return SimpleNamespace(
        success=success,
        subtype=subtype,
        text=text,
        duration=timedelta(seconds=duration),
        usage=usage,
    )


class FakeStream:
    """Async context manager + iterator yielding messages then caching a result."""

    def __init__(self, result, messages=1):
        self.result = result
        self._remaining = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._remaining <= 0:
            raise StopAsyncIteration
        self._remaining -= 1
        return SimpleNamespace(kind="message")


class FakeSession:
    """Minimal droid_sdk Session stand-in."""

    counter = 0

    def __init__(self, *, cwd=None, model=None, config=None, api_key=None):
        FakeSession.counter += 1
        self.cwd = cwd
        self.model = model
        self.config = config
        self.api_key = api_key
        self.id = f"sess-{FakeSession.counter}"
        self.opened = False
        self.closed = False
        self.stream_calls: list[str] = []
        self.next_result = make_result()

    @classmethod
    def resume(cls, session_id, **kwargs):
        resumed = cls()
        resumed.id = session_id
        return resumed

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    def stream(self, prompt, *, timeout=None, **kwargs):
        self.stream_calls.append(prompt)
        return FakeStream(self.next_result)


class TestPrompts:
    def test_fix_prompt_contains_context(self):
        prompt = build_fix_prompt("https://github.com/org/repo", 7, "Fix bug", "Details here", "main")
        assert "https://github.com/org/repo" in prompt
        assert "#7" in prompt
        assert "Fix bug" in prompt
        assert "Details here" in prompt
        assert "push" in prompt.lower()
        assert "BRANCH:" in prompt
        assert "test" in prompt.lower()

    def test_fix_prompt_empty_body(self):
        prompt = build_fix_prompt("https://github.com/org/repo", 7, "Fix bug", "", "main")
        assert "(no description provided)" in prompt

    def test_review_prompt(self):
        prompt = build_review_prompt(
            "https://github.com/org/repo", "https://github.com/org/repo/pull/3", "main"
        )
        assert "pull/3" in prompt
        assert "VERDICT: APPROVE" in prompt
        assert "review" in prompt.lower()

    def test_follow_up_prompt(self):
        prompt = build_follow_up_prompt("Fix failing CI")
        assert "Fix failing CI" in prompt
        assert "BRANCH:" in prompt


class TestParsing:
    def test_extract_branch_from_result(self):
        text = "All done.\nBRANCH: droid/fix-issue-42\nSummary of changes."
        assert extract_branch_name(text) == "droid/fix-issue-42"

    def test_extract_branch_missing(self):
        assert extract_branch_name("no branch line here") is None
        assert extract_branch_name(None) is None

    def test_extract_verdict(self):
        assert extract_verdict("Findings.\nVERDICT: APPROVE") == "APPROVE"
        assert extract_verdict("verdict: request_changes") == "REQUEST_CHANGES"
        assert extract_verdict("no verdict") is None

    def test_map_outcome_status(self):
        assert map_outcome_status({"success": True}) == TaskStatus.COMPLETED
        assert map_outcome_status({"success": False, "subtype": "error_during_execution"}) == TaskStatus.FAILED


class TestWorkspaceDirs:
    def test_task_workspace_dir(self):
        path = task_workspace_dir("jan21deepak/omnigent", 12)
        assert path.name == "task-12"
        assert "jan21deepak" in str(path)
        assert "omnigent" in str(path)

    def test_review_workspace_dir(self):
        path = review_workspace_dir("jan21deepak/omnigent", 3)
        assert path.name == "review-3"


def make_bare_repo(base: Path) -> Path:
    """Create a local bare git repo with one commit, for clone tests."""
    src = base / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=src, check=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "init"],
        cwd=src,
        check=True,
    )
    bare = base / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


class TestWorkspaces:
    @pytest.mark.asyncio
    async def test_prepare_fix_workspace_clones_and_reuses(self, tmp_path, monkeypatch):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository, token: str(bare))

        client = DroidClient(api_key="fk_test")
        ws = await client.prepare_fix_workspace(
            repository="jan21deepak/demo", task_id=5, starting_ref="main"
        )
        assert (ws / ".git").exists()
        assert (ws / "README.md").read_text() == "hello"

        # Second prepare for the same task reuses the workspace, not a re-clone.
        ws2 = await client.prepare_fix_workspace(
            repository="jan21deepak/demo", task_id=5, starting_ref="main"
        )
        assert ws2 == ws
        assert (ws2 / "README.md").exists()

    @pytest.mark.asyncio
    async def test_prepare_fix_workspace_runs_setup_command(self, tmp_path, monkeypatch):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository, token: str(bare))

        client = DroidClient(api_key="fk_test")
        ws = await client.prepare_fix_workspace(
            repository="jan21deepak/demo",
            task_id=9,
            starting_ref="main",
            setup_command="echo setup-ok > setup_marker.txt",
        )
        assert (ws / "setup_marker.txt").read_text().strip() == "setup-ok"


class TestClient:
    def test_configured(self):
        assert not DroidClient(api_key="").configured
        assert DroidClient(api_key="fk_test").configured

    def test_configured_via_cli_auth(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr("app.droid_client.shutil.which", lambda name: "/usr/local/bin/droid")
        monkeypatch.setenv("FACTORY_API_KEY", "")
        monkeypatch.setenv("DROID_ALLOW_CLI_AUTH", "true")
        get_settings.cache_clear()
        assert DroidClient().configured is True

        monkeypatch.setenv("DROID_ALLOW_CLI_AUTH", "false")
        get_settings.cache_clear()
        assert DroidClient().configured is False

    @pytest.mark.asyncio
    async def test_check_connectivity(self, monkeypatch):
        async def fake_models(**kwargs):
            return []

        monkeypatch.setattr(droid_client, "list_models", fake_models)
        assert await DroidClient(api_key="fk_test").check_connectivity() is True
        assert await DroidClient(api_key="").check_connectivity() is False

        async def boom(**kwargs):
            raise RuntimeError("nope")

        monkeypatch.setattr(droid_client, "list_models", boom)
        assert await DroidClient(api_key="fk_test").check_connectivity() is False

    @pytest.mark.asyncio
    async def test_list_available_models(self, monkeypatch):
        async def fake_models(**kwargs):
            return [
                SimpleNamespace(
                    id="auto", display_name="Auto Model", model_provider="FACTORY", disabled=False
                ),
                SimpleNamespace(
                    id="hidden", display_name="Hidden", model_provider="FACTORY", disabled=True
                ),
            ]

        monkeypatch.setattr(droid_client, "list_models", fake_models)
        models = await DroidClient(api_key="fk_test").list_available_models()
        assert [m["id"] for m in models] == ["auto"]
        assert models[0]["provider"] == "FACTORY"

    @pytest.mark.asyncio
    async def test_start_fix_run_opens_session_and_runs_turn(self, monkeypatch):
        monkeypatch.setattr(droid_client, "Session", FakeSession)
        monkeypatch.setattr(
            DroidClient,
            "prepare_fix_workspace",
            lambda self, **kwargs: asyncio.sleep(0, result=Path("/tmp/ws-demo")),
        )
        outcomes = []

        async def on_complete(outcome):
            outcomes.append(outcome)

        client = DroidClient(api_key="fk_test", model="auto")
        result = await client.start_fix_run(
            repository="jan21deepak/omnigent",
            repository_url="https://github.com/jan21deepak/omnigent",
            task_id=1,
            issue_number=7,
            issue_title="Fix bug",
            issue_body="Details",
            starting_ref="main",
            on_complete=on_complete,
        )
        assert result["session_id"].startswith("sess-")
        assert result["status"] == "running"

        # The turn runs in a background task; give it a moment to finish.
        for _ in range(50):
            if outcomes:
                break
            await asyncio.sleep(0.02)
        assert outcomes, "turn completion callback should have run"
        outcome = outcomes[0]
        assert outcome["success"] is True
        assert outcome["session_id"] == result["session_id"]
        assert outcome["duration_seconds"] == 90.0
        assert outcome["credits"] == 1.5
        assert outcome["tokens"] == 15
        assert extract_branch_name(outcome["text"]) is not None or "Done" in outcome["text"]

    @pytest.mark.asyncio
    async def test_start_fix_run_requires_configuration(self):
        client = DroidClient(api_key="")
        with pytest.raises(DroidRunError):
            await client.start_fix_run(
                repository="a/b",
                repository_url="https://github.com/a/b",
                task_id=1,
                issue_number=1,
                issue_title="t",
                issue_body="b",
            )

    @pytest.mark.asyncio
    async def test_send_follow_up_busy_and_happy_path(self, monkeypatch):
        monkeypatch.setattr(droid_client, "Session", FakeSession)
        client = DroidClient(api_key="fk_test")

        # Simulate an in-flight run for the session.
        async def forever():
            await asyncio.sleep(3600)

        client.track("sess-busy", asyncio.create_task(forever()))
        with pytest.raises(DroidSessionBusyError):
            await client.send_follow_up("sess-busy", "Fix CI")
        client._tasks["sess-busy"].cancel()

        result = await client.send_follow_up("sess-idle", "Fix CI")
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_shutdown_cancels_runs(self, monkeypatch):
        monkeypatch.setattr(droid_client, "Session", FakeSession)
        monkeypatch.setattr(
            DroidClient,
            "prepare_fix_workspace",
            lambda self, **kwargs: asyncio.sleep(0, result=Path("/tmp/ws-demo")),
        )
        client = DroidClient(api_key="fk_test")
        await client.start_fix_run(
            repository="a/b",
            repository_url="https://github.com/a/b",
            task_id=1,
            issue_number=1,
            issue_title="t",
            issue_body="b",
        )
        assert client._tasks
        await client.shutdown()
        assert all(task.done() for task in client._tasks.values())
