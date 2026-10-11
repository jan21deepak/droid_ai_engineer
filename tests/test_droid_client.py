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
    extract_branch_name,
    map_outcome_status,
    review_workspace_dir,
    task_workspace_dir,
)
from app.models import TaskStatus


def make_result(*, success=True, text="Done", duration=90.0, credits=1.5, subtype="success"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_tokens=0, factory_credits=credits)
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

    def __init__(self, *, cwd=None, model=None, config=None, runtime=None, api_key=None):
        FakeSession.counter += 1
        self.runtime = runtime
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


@pytest.fixture(autouse=True)
def no_real_droid_process(monkeypatch):
    """Unit tests never spawn droid; the clean-env launcher is tested in test_agent_env.py."""

    async def fake_runtime(*, cwd, api_key):
        return droid_client.Runtime()

    monkeypatch.setattr(droid_client, "open_agent_runtime", fake_runtime)


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

    def test_follow_up_prompt(self):
        prompt = build_follow_up_prompt("Fix failing CI")
        assert "Fix failing CI" in prompt
        assert "BRANCH:" in prompt

    @pytest.mark.parametrize(
        "prompt",
        [
            build_fix_prompt("https://github.com/org/repo", 7, "Fix bug", "Details", "main"),
            build_follow_up_prompt("Add a regression test"),
            droid_client.RECOVERY_PROMPT_TEMPLATE,
        ],
        ids=["fix", "follow_up", "recovery"],
    )
    def test_agent_prompts_describe_a_local_candidate_without_publication_authority(self, prompt):
        lowered = prompt.lower()
        assert "do not push" in lowered
        assert "open a pull request" in lowered  # only as a prohibition
        for phrase in ("automation publishes", "publishes it when permitted", "pushes it", "opens a same-repo"):
            assert phrase not in lowered


class TestParsing:
    def test_extract_branch_from_result(self):
        text = "All done.\nBRANCH: droid/fix-issue-42\nSummary of changes."
        assert extract_branch_name(text) == "droid/fix-issue-42"

    def test_extract_branch_missing(self):
        assert extract_branch_name("no branch line here") is None
        assert extract_branch_name(None) is None

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


def make_pull_request(base: Path, *, content: str) -> tuple[Path, str, str]:
    """Bare repo with ``main`` plus a feature commit published as ``refs/pull/7/head``."""
    bare = make_bare_repo(base)
    work = base / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    base_sha = run("rev-parse", "HEAD")
    run("checkout", "-q", "-b", "feature")
    (work / "feature.txt").write_text(content)
    run("add", ".")
    run("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "feature")
    head = run("rev-parse", "HEAD")
    run("push", "-q", "origin", "feature")
    subprocess.run(["git", "-C", str(bare), "update-ref", "refs/pull/7/head", head], check=True)
    return bare, base_sha, head


class TestWorkspaces:
    @pytest.mark.asyncio
    async def test_prepare_fix_workspace_clones_and_reuses(self, tmp_path, monkeypatch):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        client = DroidClient(api_key="fk_test")
        ws = await client.prepare_fix_workspace(repository="jan21deepak/demo", task_id=5, starting_ref="main")
        assert (ws / ".git").exists()
        assert (ws / "README.md").read_text() == "hello"

        # Second prepare for the same task reuses the workspace, not a re-clone.
        ws2 = await client.prepare_fix_workspace(repository="jan21deepak/demo", task_id=5, starting_ref="main")
        assert ws2 == ws
        assert (ws2 / "README.md").exists()

    @pytest.mark.asyncio
    async def test_prepare_fix_workspace_runs_setup_command(self, tmp_path, monkeypatch):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("FORGE_ALLOW_PRIVILEGED_SETUP", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        client = DroidClient(api_key="fk_test")
        ws = await client.prepare_fix_workspace(
            repository="jan21deepak/demo",
            task_id=9,
            starting_ref="main",
            setup_command="echo setup-ok > setup_marker.txt",
        )
        assert (ws / "setup_marker.txt").read_text().strip() == "setup-ok"

    @pytest.mark.asyncio
    async def test_review_context_is_exact_clean_head_with_complete_diff(self, tmp_path, monkeypatch):
        """The reviewer gets a clean detached checkout of the PR head SHA and the full base..head diff."""
        bare, base_sha, head_sha = make_pull_request(tmp_path, content="new feature line\n")
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        client = DroidClient(api_key="fk_test")
        ctx = await client.prepare_review_context(
            repository="jan21deepak/demo", review_id=1, pr_number=7, base_ref="main"
        )
        assert ctx.complete, ctx.incomplete_reason
        assert (ctx.base_sha, ctx.head_sha) == (base_sha, head_sha)
        assert "+new feature line" in ctx.diff
        assert "feature.txt" in ctx.diff
        git = lambda *a: subprocess.run(  # noqa: E731
            ["git", *a], cwd=ctx.workspace, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert git("rev-parse", "HEAD") == head_sha
        assert git("status", "--porcelain") == ""
        assert (ctx.workspace / "feature.txt").exists()

        # Stray files from an earlier attempt in the same review workspace are removed.
        (ctx.workspace / "leftover.txt").write_text("stale")
        ctx2 = await client.prepare_review_context(
            repository="jan21deepak/demo", review_id=1, pr_number=7, base_ref="main"
        )
        assert ctx2.complete
        assert not (ctx2.workspace / "leftover.txt").exists()

    @pytest.mark.asyncio
    async def test_reused_review_workspace_diffs_against_current_base(self, tmp_path, monkeypatch):
        """A base branch that moved since the first review must not widen the diff."""
        bare, _base_sha, _head = make_pull_request(tmp_path, content="first\n")
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))
        client = DroidClient(api_key="fk_test")
        await client.prepare_review_context(repository="jan21deepak/demo", review_id=4, pr_number=7, base_ref="main")

        work = tmp_path / "work"
        run = lambda *a: subprocess.run(  # noqa: E731
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *a],
            cwd=work,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        run("checkout", "-q", "main")
        (work / "unrelated.txt").write_text("landed on main\n")
        run("add", ".")
        run("commit", "-qm", "unrelated")
        new_base = run("rev-parse", "HEAD")
        run("push", "-q", "origin", "main")
        run("checkout", "-q", "feature")
        run("rebase", "-q", "main")
        (work / "feature.txt").write_text("second\n")
        run("commit", "-qam", "update")
        new_head = run("rev-parse", "HEAD")
        run("push", "-q", "--force", "origin", "feature")
        subprocess.run(["git", "-C", str(bare), "update-ref", "refs/pull/7/head", new_head], check=True)

        ctx = await client.prepare_review_context(
            repository="jan21deepak/demo", review_id=4, pr_number=7, base_ref="main"
        )
        assert ctx.complete, ctx.incomplete_reason
        assert (ctx.base_sha, ctx.head_sha) == (new_base, new_head)
        assert "unrelated.txt" not in ctx.diff
        assert "+second" in ctx.diff

    @pytest.mark.asyncio
    async def test_oversized_diff_is_incomplete_not_truncated(self, tmp_path, monkeypatch):
        bare, _base, _head = make_pull_request(tmp_path, content="x" * 400 + "\n")
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))
        monkeypatch.setattr("app.review_policy.REVIEW_DIFF_MAX_CHARS", 200)

        ctx = await DroidClient(api_key="fk_test").prepare_review_context(
            repository="jan21deepak/demo", review_id=2, pr_number=7, base_ref="main"
        )
        assert not ctx.complete
        assert "above the 200-character review limit" in ctx.incomplete_reason
        assert len(ctx.diff) > 200  # kept whole for the record, never cut down to fit

    @pytest.mark.asyncio
    async def test_missing_base_branch_is_incomplete(self, tmp_path, monkeypatch):
        bare, _base, _head = make_pull_request(tmp_path, content="change\n")
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        ctx = await DroidClient(api_key="fk_test").prepare_review_context(
            repository="jan21deepak/demo", review_id=3, pr_number=7, base_ref="does-not-exist"
        )
        assert not ctx.complete
        assert ctx.base_sha is None
        assert "base commit" in ctx.incomplete_reason

    @pytest.mark.asyncio
    async def test_inspect_workspace_reports_candidate_commit_and_dirty_state(self, tmp_path, monkeypatch):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))
        client = DroidClient(api_key="fk_test")
        ws = await client.prepare_fix_workspace(repository="jan21deepak/demo", task_id=4, starting_ref="main")
        git = lambda *a: subprocess.run(  # noqa: E731
            ["git", *a], cwd=ws, capture_output=True, text=True, check=True
        ).stdout.strip()
        git("checkout", "-q", "-b", "droid/fix-4")
        (ws / "fix.txt").write_text("fixed")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "fix")
        head = git("rev-parse", "HEAD")

        state = await client.inspect_workspace("jan21deepak/demo", 4, "droid/fix-4")
        assert (state.head_sha, state.branch, state.dirty) == (head, "droid/fix-4", False)

        (ws / "fix.txt").write_text("edited but not committed")
        state = await client.inspect_workspace("jan21deepak/demo", 4, "no-such-branch")
        assert (state.head_sha, state.branch, state.dirty) == (head, "droid/fix-4", True)

        missing = await client.inspect_workspace("jan21deepak/demo", 999, None)
        assert missing.head_sha is None


class TestClient:
    def test_configured(self):
        assert not DroidClient(api_key="").configured
        assert DroidClient(api_key="fk_test").configured

    def test_fix_autonomy_follows_setting(self, monkeypatch):
        monkeypatch.setenv("DROID_AUTONOMY", "medium")
        get_settings.cache_clear()
        assert DroidClient(api_key="fk_test").autonomy() == droid_client.Autonomy.MEDIUM

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
                SimpleNamespace(id="auto", display_name="Auto Model", model_provider="FACTORY", disabled=False),
                SimpleNamespace(id="hidden", display_name="Hidden", model_provider="FACTORY", disabled=True),
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
            "prepare_fix_workspace_with_setup",
            lambda self, **kwargs: asyncio.sleep(0, result=(Path("/tmp/ws-demo"), "not_configured")),
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
            "prepare_fix_workspace_with_setup",
            lambda self, **kwargs: asyncio.sleep(0, result=(Path("/tmp/ws-demo"), "not_configured")),
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
