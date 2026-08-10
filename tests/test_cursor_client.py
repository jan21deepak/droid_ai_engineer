"""Tests for the Cursor SDK-backed client."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cursor_client import (
    CursorClient,
    build_follow_up_prompt,
    build_prompt,
    build_review_prompt,
    extract_agent_branch,
    extract_pull_request_url,
    map_run_status,
    _run_to_dict,
)
from app.models import TaskStatus


class TestPrompt:
    def test_prompt_contains_context(self):
        prompt = build_prompt("https://github.com/org/repo", 7, "Fix bug", "Details here")
        assert "https://github.com/org/repo" in prompt
        assert "#7" in prompt
        assert "Fix bug" in prompt
        assert "Details here" in prompt
        assert "pull request" in prompt.lower()
        assert "upstream" in prompt.lower()
        assert "test" in prompt.lower()

    def test_prompt_empty_body(self):
        prompt = build_prompt("https://github.com/org/repo", 7, "Fix bug", "")
        assert "(no description provided)" in prompt

    def test_review_prompt(self):
        prompt = build_review_prompt(
            "https://github.com/org/repo", "https://github.com/org/repo/pull/3"
        )
        assert "pull/3" in prompt
        assert "review" in prompt.lower()

    def test_follow_up_prompt(self):
        prompt = build_follow_up_prompt("Fix failing CI")
        assert "Fix failing CI" in prompt


class TestStatusMapping:
    def test_creating_maps_to_queued(self):
        assert map_run_status("CREATING", False) == TaskStatus.QUEUED
        assert map_run_status("creating", False) == TaskStatus.QUEUED

    def test_running_maps_to_running(self):
        assert map_run_status("RUNNING", False) == TaskStatus.RUNNING
        assert map_run_status("running", False) == TaskStatus.RUNNING

    def test_finished_maps_to_completed(self):
        assert map_run_status("FINISHED", True) == TaskStatus.COMPLETED
        assert map_run_status("finished", True) == TaskStatus.COMPLETED

    def test_error_maps_to_failed(self):
        assert map_run_status("ERROR", False) == TaskStatus.FAILED
        assert map_run_status("error", False) == TaskStatus.FAILED

    def test_cancelled_maps_to_failed(self):
        assert map_run_status("CANCELLED", False) == TaskStatus.FAILED

    def test_merged_pr_completes(self):
        assert map_run_status("RUNNING", True, pr_merged=True) == TaskStatus.COMPLETED

    def test_unknown_defaults_to_running(self):
        assert map_run_status("something-new", False) == TaskStatus.RUNNING

    def test_map_review_status(self):
        assert CursorClient.map_review_status("creating") == TaskStatus.RUNNING
        assert CursorClient.map_review_status("running") == TaskStatus.RUNNING
        assert CursorClient.map_review_status("finished") == TaskStatus.COMPLETED
        assert CursorClient.map_review_status("error") == TaskStatus.FAILED


class TestExtractPr:
    def test_extract_from_run_git(self):
        run = {
            "git": {
                "branches": [
                    {
                        "repoUrl": "github.com/org/repo",
                        "branch": "cursor/fix",
                        "prUrl": "https://github.com/org/repo/pull/9",
                    }
                ]
            }
        }
        assert extract_pull_request_url(run) == "https://github.com/org/repo/pull/9"

    def test_extract_branch_from_git(self):
        run = {
            "git": {
                "branches": [
                    {
                        "repoUrl": "github.com/org/repo",
                        "branch": "cursor/android-switcher-height-0e0c",
                        "prUrl": "",
                    }
                ]
            }
        }
        assert extract_agent_branch(run) == "cursor/android-switcher-height-0e0c"

    def test_extract_branch_from_result_text(self):
        run = {
            "result": (
                "Branch cursor/android-switcher-height-0e0c is pushed, but PR "
                "creation failed due to repository permissions (must be a collaborator)."
            )
        }
        assert extract_agent_branch(run) == "cursor/android-switcher-height-0e0c"

    def test_extract_branch_from_manual_pull_new_link(self):
        run = {
            "result": (
                "PR creation failed due to repository permissions. Open it manually:\n"
                "https://github.com/jan21deepak/omnigent/pull/new/cursor/fix-project-hover-new-session-5c45"
            )
        }
        assert extract_agent_branch(run) == "cursor/fix-project-hover-new-session-5c45"

    def test_run_to_dict_from_sdk_objects(self):
        branch = SimpleNamespace(
            repo_url="github.com/org/repo",
            branch="cursor/fix",
            pr_url="https://github.com/org/repo/pull/9",
        )
        run = SimpleNamespace(
            id="run-1",
            agent_id="bc-1",
            status="finished",
            result="Done",
            duration_ms=5000,
            git=SimpleNamespace(branches=[branch]),
            created_at="2026-08-10T00:00:00Z",
        )
        data = _run_to_dict(run)
        assert data["id"] == "run-1"
        assert data["status"] == "finished"
        assert data["durationMs"] == 5000
        assert data["git"]["branches"][0]["prUrl"].endswith("/pull/9")


class TestClientSdk:
    @pytest.mark.asyncio
    async def test_create_agent_uses_sdk(self, monkeypatch):
        fake_run = SimpleNamespace(
            id="run-1",
            agent_id="bc-abc",
            status="running",
            result="",
            duration_ms=0,
            git=None,
            created_at=None,
        )
        fake_agent = MagicMock()
        fake_agent.agent_id = "bc-abc"
        fake_agent.send.return_value = fake_run

        create_mock = MagicMock(return_value=fake_agent)
        monkeypatch.setattr(
            "app.cursor_client.Agent.create",
            create_mock,
        )

        client = CursorClient(api_key="crsr_test", model="composer-2.5", name_prefix="cursor-forge")
        result = await client.create_agent(
            "do the thing",
            repository_url="https://github.com/org/repo",
            name="demo",
        )
        assert result["agent_id"] == "bc-abc"
        assert result["run_id"] == "run-1"
        assert "cursor.com/agents" in result["url"]
        fake_agent.send.assert_called_once()
        fake_agent.close.assert_called_once()

        create_kwargs = create_mock.call_args.kwargs
        assert create_kwargs["api_key"] == "crsr_test"
        assert create_kwargs["model"] == "composer-2.5"
        assert create_kwargs["cloud"].auto_create_pr is True
        assert create_kwargs["cloud"].env is None
        assert create_kwargs["cloud"].repos[0].url == "https://github.com/org/repo"

    @pytest.mark.asyncio
    async def test_create_agent_uses_named_environment(self, monkeypatch):
        fake_run = SimpleNamespace(
            id="run-2",
            agent_id="bc-env",
            status="running",
            result="",
            duration_ms=0,
            git=None,
            created_at=None,
        )
        fake_agent = MagicMock()
        fake_agent.agent_id = "bc-env"
        fake_agent.send.return_value = fake_run
        create_mock = MagicMock(return_value=fake_agent)
        monkeypatch.setattr("app.cursor_client.Agent.create", create_mock)

        client = CursorClient(api_key="crsr_test", model="composer-2.5")
        result = await client.create_agent(
            "do the thing",
            repository_url="https://github.com/jan21deepak/omnigent",
            name="omnigent-fix",
            environment="omnigent",
        )
        assert result["agent_id"] == "bc-env"
        assert result["environment"] == "omnigent"
        cloud = create_mock.call_args.kwargs["cloud"]
        assert cloud.env is not None
        assert cloud.env.type == "cloud"
        assert cloud.env.name == "omnigent"
        assert cloud.repos is None

    @pytest.mark.asyncio
    async def test_review_agent_keeps_repos_even_with_environment(self, monkeypatch):
        fake_run = SimpleNamespace(
            id="run-3",
            agent_id="bc-rev",
            status="running",
            result="",
            duration_ms=0,
            git=None,
            created_at=None,
        )
        fake_agent = MagicMock()
        fake_agent.agent_id = "bc-rev"
        fake_agent.send.return_value = fake_run
        create_mock = MagicMock(return_value=fake_agent)
        monkeypatch.setattr("app.cursor_client.Agent.create", create_mock)

        client = CursorClient(api_key="crsr_test")
        await client.create_review_agent(
            repository_url="https://github.com/jan21deepak/omnigent",
            pr_url="https://github.com/jan21deepak/omnigent/pull/9",
            environment="omnigent",
        )
        cloud = create_mock.call_args.kwargs["cloud"]
        assert cloud.env is None
        assert cloud.repos[0].pr_url.endswith("/pull/9")
        assert cloud.auto_create_pr is False

    @pytest.mark.asyncio
    async def test_list_environments_dedupes_named_envs(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "items": [
                        {
                            "env": {"type": "cloud", "name": "omnigent"},
                            "repos": [{"url": "https://github.com/jan21deepak/omnigent"}],
                        },
                        {
                            "env": {"type": "cloud", "name": "superset"},
                            "repos": [{"url": "https://github.com/jan21deepak/superset"}],
                        },
                        {
                            "env": {"type": "cloud", "name": "omnigent"},
                            "repos": [{"url": "https://github.com/jan21deepak/omnigent"}],
                        },
                        {"env": {"type": "cloud"}},
                    ]
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                return FakeResponse()

        monkeypatch.setattr("app.cursor_client.httpx.Client", FakeClient)
        client = CursorClient(api_key="crsr_test")
        envs = await client.list_environments()
        assert [e["name"] for e in envs] == ["omnigent", "superset"]
        assert envs[0]["repositories"] == ["https://github.com/jan21deepak/omnigent"]


    @pytest.mark.asyncio
    async def test_get_agent_status_from_sdk_run(self, monkeypatch):
        branch = SimpleNamespace(
            repo_url="github.com/org/repo",
            branch="cursor/fix",
            pr_url="https://github.com/org/repo/pull/1",
        )
        fake_run = SimpleNamespace(
            id="run-1",
            agent_id="bc-abc",
            status="finished",
            result="Done",
            duration_ms=5000,
            git=SimpleNamespace(branches=[branch]),
            created_at=None,
        )
        monkeypatch.setattr(
            "app.cursor_client.Agent.get_run",
            MagicMock(return_value=fake_run),
        )
        client = CursorClient(api_key="crsr_test")
        status, pr, run = await client.get_agent_status("bc-abc", "run-1")
        assert status == TaskStatus.COMPLETED
        assert pr.endswith("/pull/1")
        assert run["result"] == "Done"

    @pytest.mark.asyncio
    async def test_send_follow_up(self, monkeypatch):
        fake_run = SimpleNamespace(
            id="run-2",
            agent_id="bc-abc",
            status="running",
            result="",
            duration_ms=0,
            git=None,
            created_at=None,
        )
        fake_agent = MagicMock()
        fake_agent.agent_id = "bc-abc"
        fake_agent.send.return_value = fake_run
        monkeypatch.setattr(
            "app.cursor_client.Agent.resume",
            MagicMock(return_value=fake_agent),
        )
        client = CursorClient(api_key="crsr_test")
        result = await client.send_follow_up("bc-abc", "Fix CI")
        assert result["run_id"] == "run-2"
        fake_agent.send.assert_called_once()
        fake_agent.close.assert_called_once()

    def test_configured(self):
        assert not CursorClient(api_key="").configured
        assert CursorClient(api_key="k").configured

    @pytest.mark.asyncio
    async def test_check_connectivity(self, monkeypatch):
        monkeypatch.setattr(
            "app.cursor_client.Cursor.models.list",
            MagicMock(return_value=[]),
        )
        assert await CursorClient(api_key="k").check_connectivity() is True

        monkeypatch.setattr(
            "app.cursor_client.Cursor.models.list",
            MagicMock(side_effect=RuntimeError("nope")),
        )
        monkeypatch.setattr(
            "app.cursor_client.Cursor.me",
            MagicMock(side_effect=RuntimeError("nope")),
        )
        assert await CursorClient(api_key="k").check_connectivity() is False
