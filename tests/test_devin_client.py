import json

import pytest
import respx
from httpx import Response

from app.devin import DevinClient, build_prompt, map_status
from app.models import TaskStatus

API = "https://api.devin.test/v3"
ORG = "org-test"


@pytest.fixture
def devin():
    return DevinClient(
        api_key="cog_test-key",
        api_base=API,
        org_id=ORG,
        create_as_user_id="",
        session_tag="devin-forge",
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


class TestStatusMapping:
    def test_working_maps_to_running(self):
        assert map_status("working", False) == TaskStatus.RUNNING

    def test_running_maps_to_running(self):
        assert map_status("running", False, "working") == TaskStatus.RUNNING

    def test_exit_maps_to_completed(self):
        assert map_status("exit", True, "finished") == TaskStatus.COMPLETED

    def test_finished_maps_to_completed(self):
        assert map_status("finished", True) == TaskStatus.COMPLETED

    def test_error_maps_to_failed(self):
        assert map_status("error", False) == TaskStatus.FAILED

    def test_suspended_maps_to_failed(self):
        assert map_status("suspended", False, "out_of_credits") == TaskStatus.FAILED

    def test_suspended_with_pr_maps_to_completed(self):
        assert map_status("suspended", True, "inactivity") == TaskStatus.COMPLETED

    def test_waiting_for_user_with_pr_maps_to_completed(self):
        assert map_status("running", True, "waiting_for_user") == TaskStatus.COMPLETED
        assert map_status("running", True, "waiting_for_approval") == TaskStatus.COMPLETED
        assert map_status("running", False, "waiting_for_user") == TaskStatus.RUNNING

    def test_still_working_with_pr_stays_running(self):
        assert map_status("running", True, "working") == TaskStatus.RUNNING

    def test_merged_pr_completes_even_while_working(self):
        assert map_status("running", True, "working", pr_merged=True) == TaskStatus.COMPLETED
        assert map_status("running", False, "working", pr_merged=True) == TaskStatus.COMPLETED

    def test_exit_without_pr_but_finished_maps_to_completed(self):
        assert map_status("exit", False, "finished") == TaskStatus.COMPLETED

    def test_stopped_without_pr_maps_to_failed(self):
        assert map_status("stopped", False) == TaskStatus.FAILED

    def test_stopped_with_pr_maps_to_completed(self):
        assert map_status("stopped", True) == TaskStatus.COMPLETED

    def test_unknown_defaults_to_running(self):
        assert map_status("something-new", False) == TaskStatus.RUNNING


class TestClient:
    @pytest.mark.asyncio
    @respx.mock
    async def test_create_session(self, devin):
        respx.post(f"{API}/organizations/{ORG}/sessions").mock(
            return_value=Response(
                200,
                json={
                    "session_id": "abc123",
                    "url": "https://app.devin.ai/sessions/abc123",
                    "status": "new",
                },
            )
        )
        result = await devin.create_session("do the thing", title="test")
        assert result["session_id"] == "abc123"

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_session_omits_impersonation_when_unset(self, devin):
        route = respx.post(f"{API}/organizations/{ORG}/sessions").mock(
            return_value=Response(200, json={"session_id": "abc123", "url": "u"})
        )
        await devin.create_session("do the thing")

        payload = json.loads(route.calls.last.request.content)
        assert "create_as_user_id" not in payload
        assert payload["tags"] == ["devin-forge"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_session_attributes_to_real_user(self):
        """Service-user keys otherwise land sessions on the bot_apk pseudo-user."""
        client = DevinClient(
            api_key="cog_test-key",
            api_base=API,
            org_id=ORG,
            create_as_user_id="user-real123",
            session_tag="devin-forge",
            max_retries=1,
        )
        route = respx.post(f"{API}/organizations/{ORG}/sessions").mock(
            return_value=Response(
                200,
                json={"session_id": "abc123", "url": "u", "user_id": "user-real123"},
            )
        )
        await client.create_session("do the thing")

        payload = json.loads(route.calls.last.request.content)
        assert payload["create_as_user_id"] == "user-real123"

    @pytest.mark.asyncio
    @respx.mock
    async def test_org_id_is_scoped_into_every_request_path(self, devin):
        respx.get(f"{API}/organizations/{ORG}/sessions/abc123").mock(
            return_value=Response(200, json={"session_id": "abc123", "status": "running"})
        )
        await devin.get_session("abc123")
        assert f"/organizations/{ORG}/" in str(respx.calls.last.request.url)

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_session_status_with_pr(self, devin):
        respx.get(f"{API}/organizations/{ORG}/sessions/abc123").mock(
            return_value=Response(
                200,
                json={
                    "session_id": "abc123",
                    "status": "exit",
                    "status_detail": "finished",
                    "pull_requests": [{"pr_url": "https://github.com/org/repo/pull/1", "pr_state": "open"}],
                },
            )
        )
        status, pr = await devin.get_session_status("abc123")
        assert status == TaskStatus.COMPLETED
        assert pr == "https://github.com/org/repo/pull/1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_session_summary_structured_output(self, devin):
        respx.get(f"{API}/organizations/{ORG}/sessions/abc123").mock(
            return_value=Response(
                200,
                json={
                    "status": "exit",
                    "structured_output": {"summary": "Fixed the bug and added tests."},
                },
            )
        )
        summary = await devin.get_session_summary("abc123")
        assert summary == "Fixed the bug and added tests."

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_session_summary_from_messages(self, devin):
        respx.get(f"{API}/organizations/{ORG}/sessions/abc123").mock(
            return_value=Response(
                200,
                json={
                    "status": "exit",
                    "messages": [
                        {"type": "user_message", "message": "please fix"},
                        {"type": "devin_message", "message": "Done. Opened PR #1."},
                    ],
                },
            )
        )
        summary = await devin.get_session_summary("abc123")
        assert summary == "Done. Opened PR #1."

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_session_summary_from_messages_endpoint(self, devin):
        respx.get(f"{API}/organizations/{ORG}/sessions/abc123").mock(
            return_value=Response(200, json={"status": "suspended", "structured_output": None})
        )
        respx.get(f"{API}/organizations/{ORG}/sessions/abc123/messages").mock(
            return_value=Response(
                200,
                json={
                    "items": [
                        {"source": "user", "message": "please fix"},
                        {"source": "devin", "message": "Fixed the legend overlap and opened a PR."},
                    ]
                },
            )
        )
        summary = await devin.get_session_summary("abc123")
        assert summary == "Fixed the legend overlap and opened a PR."

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_error_raises(self, devin):
        respx.post(f"{API}/organizations/{ORG}/sessions").mock(
            return_value=Response(401, json={"detail": "unauthorized"})
        )
        with pytest.raises(Exception):
            await devin.create_session("prompt")

    def test_unconfigured_client(self):
        assert not DevinClient(api_key="", api_base=API, org_id=ORG).configured
        assert not DevinClient(api_key="cog_x", api_base=API, org_id="").configured


class TestPrReview:
    def test_map_review_status(self):
        assert DevinClient.map_review_status("pending") == TaskStatus.QUEUED
        assert DevinClient.map_review_status("running") == TaskStatus.RUNNING
        assert DevinClient.map_review_status("completed") == TaskStatus.COMPLETED
        assert DevinClient.map_review_status("errored") == TaskStatus.FAILED
        assert DevinClient.map_review_status("cancelled") == TaskStatus.FAILED

    @pytest.mark.asyncio
    @respx.mock
    async def test_create_pr_review(self, devin):
        respx.post(f"{API}/organizations/{ORG}/pr-reviews").mock(
            return_value=Response(
                200,
                json={
                    "status": "pending",
                    "repo_path": "github.com/org/repo",
                    "pr_number": 3,
                    "commit_sha": "abc",
                    "created_at": "2026-08-02T14:00:00Z",
                },
            )
        )
        data = await devin.create_pr_review("https://github.com/org/repo/pull/3")
        assert data["status"] == "pending"
        assert data["pr_number"] == 3

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_pr_review(self, devin):
        respx.get(f"{API}/organizations/{ORG}/pr-reviews").mock(
            return_value=Response(
                200,
                json={
                    "status": "running",
                    "repo_path": "github.com/org/repo",
                    "pr_number": 3,
                    "commit_sha": "abc",
                    "created_at": "2026-08-02T14:00:00Z",
                },
            )
        )
        data = await devin.get_pr_review("https://github.com/org/repo/pull/3")
        assert data["status"] == "running"
