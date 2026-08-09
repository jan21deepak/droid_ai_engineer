"""Tests for the Cursor Cloud Agents client."""

import pytest
import respx
from httpx import Response

from app.cursor_client import (
    CursorClient,
    build_prompt,
    build_review_prompt,
    extract_pull_request_url,
    map_run_status,
)
from app.models import TaskStatus

API = "https://api.cursor.test"


@pytest.fixture
def cursor():
    return CursorClient(
        api_key="cursor_test_key",
        api_base=API,
        model="",
        name_prefix="cursor-forge",
        max_retries=1,
    )


class TestPrompt:
    def test_prompt_contains_context(self):
        prompt = build_prompt("https://github.com/org/repo", 7, "Fix bug", "Details here")
        assert "https://github.com/org/repo" in prompt
        assert "#7" in prompt
        assert "Fix bug" in prompt
        assert "Details here" in prompt
        assert "pull request" in prompt.lower()
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


class TestStatusMapping:
    def test_creating_maps_to_queued(self):
        assert map_run_status("CREATING", False) == TaskStatus.QUEUED

    def test_running_maps_to_running(self):
        assert map_run_status("RUNNING", False) == TaskStatus.RUNNING

    def test_finished_maps_to_completed(self):
        assert map_run_status("FINISHED", True) == TaskStatus.COMPLETED

    def test_error_maps_to_failed(self):
        assert map_run_status("ERROR", False) == TaskStatus.FAILED

    def test_cancelled_maps_to_failed(self):
        assert map_run_status("CANCELLED", False) == TaskStatus.FAILED

    def test_merged_pr_completes(self):
        assert map_run_status("RUNNING", True, pr_merged=True) == TaskStatus.COMPLETED

    def test_unknown_defaults_to_running(self):
        assert map_run_status("something-new", False) == TaskStatus.RUNNING


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


class TestClient:
    @pytest.mark.asyncio
    @respx.mock
    async def test_create_agent(self, cursor):
        respx.post(f"{API}/v1/agents").mock(
            return_value=Response(
                200,
                json={
                    "agent": {
                        "id": "bc-abc123",
                        "url": "https://cursor.com/agents/bc-abc123",
                        "latestRunId": "run-1",
                    },
                    "run": {"id": "run-1", "status": "CREATING"},
                },
            )
        )
        result = await cursor.create_agent(
            "do the thing",
            repository_url="https://github.com/org/repo",
            name="test",
        )
        assert result["agent_id"] == "bc-abc123"
        assert result["run_id"] == "run-1"
        assert "cursor.com/agents" in result["url"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_agent_payload(self, cursor):
        route = respx.post(f"{API}/v1/agents").mock(
            return_value=Response(
                200,
                json={
                    "agent": {"id": "bc-1", "url": "https://cursor.com/agents/bc-1"},
                    "run": {"id": "run-1", "status": "CREATING"},
                },
            )
        )
        await cursor.create_agent(
            "do the thing",
            repository_url="https://github.com/org/repo",
            name="demo",
        )
        payload = route.calls[0].request.read()
        import json

        body = json.loads(payload)
        assert body["prompt"]["text"] == "do the thing"
        assert body["repos"][0]["url"] == "https://github.com/org/repo"
        assert body["repos"][0]["startingRef"] == "main"
        assert body["autoCreatePR"] is True
        assert body["name"].startswith("cursor-forge")

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_agent_status_with_pr(self, cursor):
        respx.get(f"{API}/v1/agents/bc-abc123").mock(
            return_value=Response(
                200,
                json={"id": "bc-abc123", "latestRunId": "run-1"},
            )
        )
        respx.get(f"{API}/v1/agents/bc-abc123/runs/run-1").mock(
            return_value=Response(
                200,
                json={
                    "id": "run-1",
                    "status": "FINISHED",
                    "result": "Done",
                    "durationMs": 5000,
                    "git": {
                        "branches": [
                            {"prUrl": "https://github.com/org/repo/pull/1"}
                        ]
                    },
                },
            )
        )
        status, pr, run = await cursor.get_agent_status("bc-abc123")
        assert status == TaskStatus.COMPLETED
        assert pr.endswith("/pull/1")
        assert run["result"] == "Done"

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_review_agent(self, cursor):
        respx.post(f"{API}/v1/agents").mock(
            return_value=Response(
                200,
                json={
                    "agent": {"id": "bc-rev", "url": "https://cursor.com/agents/bc-rev"},
                    "run": {"id": "run-rev", "status": "CREATING"},
                },
            )
        )
        data = await cursor.create_review_agent(
            repository_url="https://github.com/org/repo",
            pr_url="https://github.com/org/repo/pull/3",
        )
        assert data["agent_id"] == "bc-rev"

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_error_raises(self, cursor):
        respx.post(f"{API}/v1/agents").mock(return_value=Response(400, json={"error": "bad"}))
        with pytest.raises(Exception):
            await cursor.create_agent("prompt", repository_url="https://github.com/org/repo")

    def test_configured(self):
        assert not CursorClient(api_key="", api_base=API).configured
        assert CursorClient(api_key="k", api_base=API).configured

    def test_map_review_status(self):
        # Reviews always have a PR URL, so CREATING promotes to running.
        assert CursorClient.map_review_status("CREATING") == TaskStatus.RUNNING
        assert CursorClient.map_review_status("RUNNING") == TaskStatus.RUNNING
        assert CursorClient.map_review_status("FINISHED") == TaskStatus.COMPLETED
        assert CursorClient.map_review_status("ERROR") == TaskStatus.FAILED
