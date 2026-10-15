"""Clean agent environments and transient app-owned Git credentials.

Every secret here is a fake sentinel. The tests run real subprocesses (the
launcher, ``git``) so they check what a child process actually receives, not
just what a helper returns.
"""

import asyncio
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import agent_env, droid_client
from app.agent_env import (
    LAUNCHER_PATH,
    agent_environment,
    build_agent_transport,
    clean_child_environment,
    git_environment,
)
from app.config import get_settings
from app.droid_client import DroidClient, SetupStatus, task_workspace_dir
from app.redaction import git_basic_auth_value

GITHUB_SENTINEL = "fake-gh-sentinel-7Qm2vXk9Lp"
OPERATOR_SENTINEL = "fake-operator-sentinel-0123456789-abcdefghij"
WEBHOOK_SENTINEL = "fake-webhook-sentinel-55aa66bb"
FACTORY_SENTINEL = "fk-fake-factory-sentinel-1234"
SENTINELS = (GITHUB_SENTINEL, OPERATOR_SENTINEL, WEBHOOK_SENTINEL)


@pytest.fixture
def secret_env(monkeypatch):
    """Service-process credentials that must never reach an agent."""
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_SENTINEL)
    monkeypatch.setenv("GH_TOKEN", GITHUB_SENTINEL)
    monkeypatch.setenv("FORGE_OPERATOR_TOKEN", OPERATOR_SENTINEL)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SENTINEL)
    monkeypatch.setenv("FACTORY_API_KEY", FACTORY_SENTINEL)
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/askpass-that-publishes")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    get_settings.cache_clear()


def make_bare_repo(base: Path) -> Path:
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


@pytest.fixture
def recorded_subprocesses(monkeypatch):
    """Record argv and env of every subprocess the app spawns (still runs them)."""
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    real = asyncio.create_subprocess_exec

    async def recording(*args, **kwargs):
        calls.append((tuple(str(a) for a in args), dict(kwargs.get("env") or {})))
        return await real(*args, **kwargs)

    monkeypatch.setattr(droid_client.asyncio, "create_subprocess_exec", recording)
    return calls


class TestCleanEnvironment:
    def test_only_allowlisted_names_survive(self, secret_env):
        env = clean_child_environment()
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "FORGE_OPERATOR_TOKEN", "GITHUB_WEBHOOK_SECRET", "FACTORY_API_KEY"):
            assert name not in env
        assert "GIT_ASKPASS" not in env
        assert "SSH_AUTH_SOCK" not in env
        assert env["PATH"] == os.environ["PATH"]
        assert not any(secret in value for value in env.values() for secret in SENTINELS)

    def test_unknown_new_secret_names_are_dropped_by_default(self):
        env = clean_child_environment({"PATH": "/bin", "SOME_FUTURE_SERVICE_PASSWORD": "x", "LC_ALL": "C"})
        assert env == {"PATH": "/bin", "LC_ALL": "C"}

    def test_existing_git_identity_is_kept(self):
        env = clean_child_environment({"GIT_AUTHOR_NAME": "Operator", "GIT_COMMITTER_EMAIL": "op@example.com"})
        assert env == {"GIT_AUTHOR_NAME": "Operator", "GIT_COMMITTER_EMAIL": "op@example.com"}

    def test_agent_environment_resets_credential_helpers_and_prompts(self, secret_env):
        env = agent_environment()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""
        assert not any(secret in value for value in env.values() for secret in SENTINELS)


class TestAgentTransport:
    def test_transport_command_line_carries_names_not_values(self, secret_env, tmp_path):
        transport = build_agent_transport(cwd=tmp_path, api_key=FACTORY_SENTINEL, droid_path="/usr/local/bin/droid")
        argv = [transport._exec_path, *transport._exec_args]
        assert argv[1:3] == ["-I", str(LAUNCHER_PATH)]
        assert "FACTORY_API_KEY" in argv
        assert "GITHUB_TOKEN" not in argv
        assert "FORGE_OPERATOR_TOKEN" not in argv
        joined = " ".join(argv)
        assert FACTORY_SENTINEL not in joined
        assert not any(secret in joined for secret in SENTINELS)

    @pytest.mark.asyncio
    async def test_spawned_agent_process_sees_clean_environment(self, secret_env, tmp_path):
        """ProcessTransport merges os.environ; the launcher must strip it before droid starts."""
        dump = tmp_path / "agent-env.txt"
        fake_droid = tmp_path / "fake-droid"
        fake_droid.write_text(f'#!/bin/sh\nenv > "{dump}"\necho "$@" >> "{dump}.args"\n')
        fake_droid.chmod(fake_droid.stat().st_mode | stat.S_IEXEC)

        transport = build_agent_transport(cwd=tmp_path, api_key=FACTORY_SENTINEL, droid_path=str(fake_droid))
        await transport.connect()
        try:
            for _ in range(100):
                if dump.exists() and Path(f"{dump}.args").exists():
                    break
                await asyncio.sleep(0.05)
        finally:
            await transport.close()

        child_env = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "FORGE_OPERATOR_TOKEN", "GITHUB_WEBHOOK_SECRET"):
            assert name not in child_env
        assert "GIT_ASKPASS" not in child_env
        assert "SSH_AUTH_SOCK" not in child_env
        assert not any(secret in dump.read_text() for secret in SENTINELS)
        # The Factory key is the one credential the agent runtime itself needs.
        assert child_env["FACTORY_API_KEY"] == FACTORY_SENTINEL
        assert child_env["PATH"]
        assert child_env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert Path(f"{dump}.args").read_text().split() == list(agent_env.DROID_EXEC_ARGS)

    def test_launcher_rejects_missing_program(self):
        result = subprocess.run(
            ["python3", "-I", str(LAUNCHER_PATH), "PATH"], capture_output=True, text=True, check=False
        )
        assert result.returncode == 2


class TestGitCredentialTransport:
    URL = "https://github.com/jan21deepak/allowed.git"

    def _urlmatch(self, env: dict[str, str], url: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraheader", url],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_credential_header_is_scoped_to_one_repository_url(self, secret_env, tmp_path):
        env = git_environment(token=GITHUB_SENTINEL, credential_url=self.URL)
        matched = self._urlmatch(env, self.URL, tmp_path)
        assert matched.returncode == 0
        assert matched.stdout.strip() == f"AUTHORIZATION: basic {git_basic_auth_value(GITHUB_SENTINEL)}"
        assert self._urlmatch(env, "https://github.com/someone/else.git", tmp_path).returncode != 0
        assert self._urlmatch(env, "https://attacker.example/jan21deepak/allowed.git", tmp_path).returncode != 0

    def test_without_credential_url_no_header_and_no_inherited_secrets(self, secret_env, tmp_path):
        env = git_environment()
        assert self._urlmatch(env, self.URL, tmp_path).returncode != 0
        assert not any(secret in value for value in env.values() for secret in SENTINELS)

    def test_hooks_and_fsmonitor_are_disabled_for_app_git(self):
        env = git_environment()
        hooks = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"], env=env, capture_output=True, text=True, check=False
        )
        assert hooks.stdout.strip() == os.devnull

    @pytest.mark.asyncio
    async def test_clone_keeps_token_out_of_argv_remote_and_config(
        self, tmp_path, monkeypatch, secret_env, recorded_subprocesses
    ):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        ws = await DroidClient(api_key="fk").prepare_fix_workspace(
            repository="jan21deepak/allowed", task_id=1, starting_ref="main"
        )

        encoded = git_basic_auth_value(GITHUB_SENTINEL)
        for argv, env in recorded_subprocesses:
            assert not any(GITHUB_SENTINEL in arg or encoded in arg for arg in argv)
            for name in ("GITHUB_TOKEN", "GH_TOKEN", "FORGE_OPERATOR_TOKEN", "GITHUB_WEBHOOK_SECRET"):
                assert name not in env
        clone_env = next(env for argv, env in recorded_subprocesses if "clone" in argv)
        assert any(encoded in value for value in clone_env.values()), "app-owned clone is authenticated"

        config_text = (ws / ".git" / "config").read_text()
        assert GITHUB_SENTINEL not in config_text
        assert encoded not in config_text
        assert "extraheader" not in config_text
        listed = subprocess.run(
            ["git", "config", "--list", "--show-origin"],
            cwd=ws,
            env=clean_child_environment(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert GITHUB_SENTINEL not in listed and encoded not in listed

    @pytest.mark.asyncio
    async def test_legacy_token_bearing_remote_is_replaced(self, tmp_path, monkeypatch, secret_env):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        ws = task_workspace_dir("jan21deepak/allowed", 9)
        ws.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(ws)], check=True)
        legacy = f"https://x-access-token:{GITHUB_SENTINEL}@github.com/jan21deepak/allowed.git"
        subprocess.run(["git", "remote", "add", "origin", legacy], cwd=ws, check=True)

        await DroidClient(api_key="fk").prepare_fix_workspace(
            repository="jan21deepak/allowed", task_id=9, starting_ref="main"
        )

        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=ws, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert remote == "https://github.com/jan21deepak/allowed.git"
        assert GITHUB_SENTINEL not in (ws / ".git" / "config").read_text()

    @pytest.mark.asyncio
    async def test_configured_git_identity_is_preserved(self, tmp_path, monkeypatch):
        bare = make_bare_repo(tmp_path)
        global_config = tmp_path / "gitconfig"
        global_config.write_text("[user]\n\tname = Existing Operator\n\temail = operator@example.com\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        ws = await DroidClient(api_key="fk").prepare_fix_workspace(
            repository="jan21deepak/allowed", task_id=2, starting_ref="main"
        )

        assert "Droid Forge" not in (ws / ".git" / "config").read_text()
        assert "[user]" not in (ws / ".git" / "config").read_text()
        assert global_config.read_text() == "[user]\n\tname = Existing Operator\n\temail = operator@example.com\n"


class TestGitIdentityRequired:
    """A missing or blank identity is reported, never invented or overwritten."""

    @pytest.mark.parametrize(
        "global_text",
        [
            "",
            "[user]\n\tname = Existing Operator\n",
            "[user]\n\temail = operator@example.com\n",
            '[user]\n\tname = "   "\n\temail = operator@example.com\n',
            '[user]\n\tname = Existing Operator\n\temail = ""\n',
        ],
        ids=["none", "name-only", "email-only", "blank-name", "blank-email"],
    )
    @pytest.mark.asyncio
    async def test_unusable_identity_fails_before_setup_without_identity_writes(
        self, tmp_path, monkeypatch, recorded_subprocesses, global_text
    ):
        bare = make_bare_repo(tmp_path)
        global_config = tmp_path / "gitconfig"
        global_config.write_text(global_text)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        monkeypatch.setenv("FORGE_ALLOW_PRIVILEGED_SETUP", "true")
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))
        opened: list[dict] = []

        async def fake_open(self, **kwargs):
            opened.append(kwargs)
            raise AssertionError("no SDK session may open without a usable Git identity")

        monkeypatch.setattr(DroidClient, "_open_session", fake_open)

        with pytest.raises(droid_client.DroidRunError, match="Git identity"):
            await DroidClient(api_key="fk").start_fix_run(
                repository="jan21deepak/allowed",
                repository_url="https://github.com/jan21deepak/allowed",
                task_id=6,
                issue_number=1,
                issue_title="t",
                issue_body="b",
                starting_ref="main",
                setup_command="touch SETUP_RAN",
            )

        ws = task_workspace_dir("jan21deepak/allowed", 6)
        assert opened == []
        assert not (ws / "SETUP_RAN").exists()
        assert global_config.read_text() == global_text
        local = (ws / ".git" / "config").read_text()
        assert "[user]" not in local and "Droid Forge" not in local
        for argv, _env in recorded_subprocesses:
            assert not ("config" in argv and any(arg.startswith("user.") for arg in argv) and "--get" not in argv), argv
            assert argv[0] == "git"

    @pytest.mark.asyncio
    async def test_identity_from_environment_is_usable(self, tmp_path, monkeypatch):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Env Operator")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "env@example.com")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Env Operator")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "env@example.com")
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        ws = await DroidClient(api_key="fk").prepare_fix_workspace(
            repository="jan21deepak/allowed", task_id=7, starting_ref="main"
        )
        assert "[user]" not in (ws / ".git" / "config").read_text()


class TestPrivilegedSetup:
    @pytest.mark.asyncio
    async def test_stored_setup_command_does_not_run_by_default(self, tmp_path, monkeypatch, recorded_subprocesses):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        ws, setup = await DroidClient(api_key="fk").prepare_fix_workspace_with_setup(
            repository="jan21deepak/allowed", task_id=3, starting_ref="main", setup_command="touch SETUP_RAN"
        )

        assert setup == SetupStatus.DISABLED
        assert not (ws / "SETUP_RAN").exists()
        assert all(argv[0] == "git" for argv, _env in recorded_subprocesses)

    @pytest.mark.asyncio
    async def test_opted_in_setup_runs_with_clean_environment(self, tmp_path, monkeypatch, secret_env):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("FORGE_ALLOW_PRIVILEGED_SETUP", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))

        ws, setup = await DroidClient(api_key="fk").prepare_fix_workspace_with_setup(
            repository="jan21deepak/allowed", task_id=4, starting_ref="main", setup_command="env > setup-env.txt"
        )

        assert setup == SetupStatus.SUCCEEDED
        dumped = (ws / "setup-env.txt").read_text()
        assert not any(secret in dumped for secret in (*SENTINELS, FACTORY_SENTINEL))
        assert "GITHUB_TOKEN=" not in dumped

    @pytest.mark.asyncio
    async def test_fix_run_reports_disabled_setup(self, tmp_path, monkeypatch):
        bare = make_bare_repo(tmp_path)
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
        get_settings.cache_clear()
        monkeypatch.setattr(droid_client, "_remote_url", lambda repository: str(bare))
        opened: list[dict] = []

        async def fake_open(self, **kwargs):
            opened.append(kwargs)
            raise droid_client.DroidRunError("stop before any SDK call")

        monkeypatch.setattr(DroidClient, "_open_session", fake_open)
        client = DroidClient(api_key="fk")
        with pytest.raises(droid_client.DroidRunError):
            await client.start_fix_run(
                repository="jan21deepak/allowed",
                repository_url="https://github.com/jan21deepak/allowed",
                task_id=5,
                issue_number=1,
                issue_title="t",
                issue_body="b",
                starting_ref="main",
                setup_command="touch SETUP_RAN",
            )
        assert opened, "session opening was reached after workspace preparation"
        assert not (task_workspace_dir("jan21deepak/allowed", 5) / "SETUP_RAN").exists()

    def test_repository_api_shows_stored_but_disabled_setup(self, client, monkeypatch):
        from app.database import db_session
        from app.models import Repository

        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", "jan21deepak/allowed")
        get_settings.cache_clear()
        spawned: list[tuple] = []

        async def no_spawn(*args, **kwargs):
            spawned.append(args)
            raise AssertionError("no subprocess may be launched by a repository update")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
        with db_session() as session:
            repo = Repository(full_name="jan21deepak/allowed", url="https://github.com/jan21deepak/allowed")
            session.add(repo)
            session.flush()
            repo_id = repo.id

        resp = client.patch(f"/api/repositories/{repo_id}", json={"setup_command": "npm ci"})
        assert resp.status_code == 200
        body = resp.json()["repository"]
        assert body["setup_command"] == "npm ci"
        assert body["setup_command_execution"] == "disabled_by_policy"
        listed = client.get("/api/repositories").json()["repositories"][0]
        assert listed["setup_command"] == "npm ci"
        assert listed["setup_command_execution"] == "disabled_by_policy"
        assert spawned == []

        monkeypatch.setenv("FORGE_ALLOW_PRIVILEGED_SETUP", "true")
        get_settings.cache_clear()
        assert client.get("/api/repositories").json()["repositories"][0]["setup_command_execution"] == "enabled"
