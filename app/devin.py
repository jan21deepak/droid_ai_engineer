"""Reusable async client for the Devin API (https://docs.devin.ai).

Supports the v3 service-user API (cog_* keys + organization scope).
"""

import asyncio
import logging

import httpx

from app.config import get_settings
from app.logging_conf import log_event
from app.models import TaskStatus

logger = logging.getLogger("app.devin")

PROMPT_TEMPLATE = """You are working on the repository: {repository_url}

Resolve the following GitHub issue (#{issue_number}):

Title: {issue_title}

Description:
{issue_body}

Requirements:
- Make only the requested changes described in the issue.
- Run the project's test suite.
- Fix any failures introduced by your changes.
- Create a pull request with a clear title and description referencing issue #{issue_number}.
- Summarize the completed work at the end.
"""

# Devin v3 status / status_detail / legacy status_enum -> internal task status
_STATUS_MAP = {
    # v3 top-level status
    "new": TaskStatus.QUEUED,
    "claimed": TaskStatus.QUEUED,
    "running": TaskStatus.RUNNING,
    "resuming": TaskStatus.RUNNING,
    "exit": TaskStatus.COMPLETED,
    "error": TaskStatus.FAILED,
    "suspended": TaskStatus.FAILED,
    # v3 status_detail / legacy status_enum
    "working": TaskStatus.RUNNING,
    "waiting_for_user": TaskStatus.RUNNING,
    "waiting_for_approval": TaskStatus.RUNNING,
    "finished": TaskStatus.COMPLETED,
    "stopped": TaskStatus.COMPLETED,
    "expired": TaskStatus.FAILED,
    "failed": TaskStatus.FAILED,
    "queued": TaskStatus.QUEUED,
    "initializing": TaskStatus.QUEUED,
    "resumed": TaskStatus.RUNNING,
    "suspend_requested": TaskStatus.RUNNING,
    "suspend_requested_frontend": TaskStatus.RUNNING,
    "resume_requested": TaskStatus.RUNNING,
    "blocked": TaskStatus.RUNNING,
}


def build_prompt(repository_url: str, issue_number: int, issue_title: str, issue_body: str) -> str:
    return PROMPT_TEMPLATE.format(
        repository_url=repository_url,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body or "(no description provided)",
    )


def map_status(
    status: str | None,
    has_pull_request: bool,
    status_detail: str | None = None,
    *,
    pr_merged: bool = False,
) -> str:
    """Map Devin session status fields to an internal TaskStatus.

    Devin often stays ``running`` with ``waiting_for_user`` long after a PR is
    open (and even after it merges). Once a PR exists and Devin is no longer
    actively coding, treat the fix as completed.
    """
    top = (status or "").lower()
    detail = (status_detail or "").lower()

    if top in ("error", "expired", "failed"):
        return TaskStatus.FAILED

    # Merged PR is definitive: the deliverable landed regardless of session state.
    if pr_merged or (has_pull_request and detail in ("waiting_for_user", "waiting_for_approval")):
        return TaskStatus.COMPLETED

    # A session that produced a pull request did its job, even if Devin later
    # suspended it for inactivity or the run exited without a "finished" detail.
    if has_pull_request and top not in ("new", "claimed", "running", "resuming"):
        return TaskStatus.COMPLETED
    if top == "suspended":
        return TaskStatus.COMPLETED if detail == "finished" else TaskStatus.FAILED
    if top == "stopped" and not has_pull_request:
        return TaskStatus.FAILED
    if top == "exit":
        return TaskStatus.COMPLETED if detail in ("finished", "") else TaskStatus.FAILED
    if top in _STATUS_MAP:
        mapped = _STATUS_MAP[top]
        # running + finished detail means the agent is done wrapping up
        if top == "running" and detail == "finished" and has_pull_request:
            return TaskStatus.COMPLETED
        return mapped

    mapped = _STATUS_MAP.get(detail, TaskStatus.RUNNING)
    if mapped == TaskStatus.COMPLETED and not has_pull_request and detail == "stopped":
        return TaskStatus.FAILED
    return mapped


def session_has_merged_pr(data: dict) -> bool:
    """True when Devin reports at least one merged pull request on the session."""
    for item in data.get("pull_requests") or []:
        if not isinstance(item, dict):
            continue
        state = (item.get("pr_state") or item.get("state") or "").lower()
        if state == "merged":
            return True
    return False


def extract_pull_request_url(data: dict) -> str | None:
    """Support both v3 `pull_requests[]` and legacy `pull_request` shapes."""
    prs = data.get("pull_requests") or []
    if isinstance(prs, list):
        for item in prs:
            if isinstance(item, dict):
                url = item.get("pr_url") or item.get("url")
                if url:
                    return url
    pr = data.get("pull_request") or {}
    if isinstance(pr, dict):
        return pr.get("url") or pr.get("pr_url")
    return None


class DevinClient:
    """Async client for creating and monitoring Devin sessions (v3 API)."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        org_id: str | None = None,
        create_as_user_id: str | None = None,
        session_tag: str | None = None,
        max_retries: int = 3,
        timeout: float = 30.0,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.devin_api_key
        self.api_base = (api_base or settings.devin_api_base).rstrip("/")
        self.org_id = org_id if org_id is not None else settings.devin_org_id
        self.create_as_user_id = (
            create_as_user_id
            if create_as_user_id is not None
            else settings.devin_create_as_user_id
        )
        self.session_tag = (
            session_tag if session_tag is not None else settings.devin_session_tag
        )
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.org_id)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _sessions_path(self, session_id: str | None = None) -> str:
        base = f"/organizations/{self.org_id}/sessions"
        return f"{base}/{session_id}" if session_id else base

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.api_base}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.request(method, url, headers=self._headers(), **kwargs)
                    resp.raise_for_status()
                    return resp
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            if attempt < self.max_retries:
                await asyncio.sleep(2 ** attempt)
        raise last_exc  # type: ignore[misc]

    async def create_session(self, prompt: str, title: str | None = None) -> dict:
        """Create a Devin session. Returns {'session_id': ..., 'url': ...}."""
        payload: dict = {"prompt": prompt}
        if title:
            payload["title"] = title
        if self.create_as_user_id:
            payload["create_as_user_id"] = self.create_as_user_id
        if self.session_tag:
            payload["tags"] = [self.session_tag]
        resp = await self._request("POST", self._sessions_path(), json=payload)
        data = resp.json()
        log_event(
            logger,
            logging.INFO,
            "devin.session_created",
            session_id=data.get("session_id"),
            url=data.get("url"),
            user_id=data.get("user_id"),
            created_as=self.create_as_user_id or "service-user",
        )
        return data

    async def get_session(self, session_id: str) -> dict:
        """Retrieve full session detail including status and pull requests."""
        resp = await self._request("GET", self._sessions_path(session_id))
        return resp.json()

    async def get_session_status(self, session_id: str) -> tuple[str, str | None]:
        """Return (internal_status, pull_request_url) for a session."""
        data = await self.get_session(session_id)
        pr_url = extract_pull_request_url(data)
        # Prefer top-level v3 status; fall back to legacy status_enum
        status_value = data.get("status") or data.get("status_enum")
        status = map_status(status_value, bool(pr_url), data.get("status_detail"))
        return status, pr_url

    async def get_session_messages(self, session_id: str) -> list[dict]:
        """Fetch the session transcript (v3 returns a paginated `items` list)."""
        try:
            resp = await self._request("GET", f"{self._sessions_path(session_id)}/messages")
        except httpx.HTTPError:
            return []
        data = resp.json()
        if isinstance(data, dict):
            return data.get("items") or data.get("messages") or []
        return data if isinstance(data, list) else []

    async def get_session_summary(self, session_id: str) -> str | None:
        """Best-effort extraction of a human-readable summary for the session."""
        data = await self.get_session(session_id)
        structured = data.get("structured_output")
        if isinstance(structured, dict):
            for key in ("summary", "result", "output"):
                if structured.get(key):
                    return str(structured[key])
        elif isinstance(structured, str) and structured.strip():
            return structured

        messages = data.get("messages") or await self.get_session_messages(session_id)
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            is_devin = message.get("source") == "devin" or message.get("type") == "devin_message"
            if is_devin and message.get("message"):
                return message["message"]
        return data.get("title")

    def _pr_reviews_path(self) -> str:
        return f"/organizations/{self.org_id}/pr-reviews"

    @staticmethod
    def map_review_status(status: str | None) -> str:
        """Map Devin Review API status onto internal TaskStatus values."""
        value = (status or "").lower()
        if value in ("pending",):
            return TaskStatus.QUEUED
        if value in ("running",):
            return TaskStatus.RUNNING
        if value in ("completed",):
            return TaskStatus.COMPLETED
        if value in ("errored", "cancelled", "canceled"):
            return TaskStatus.FAILED
        return TaskStatus.RUNNING

    async def create_pr_review(self, pr_url: str) -> dict:
        """Trigger Devin Review for a pull request. Returns PrReviewResponse."""
        resp = await self._request("POST", self._pr_reviews_path(), json={"pr_url": pr_url})
        data = resp.json()
        log_event(
            logger,
            logging.INFO,
            "devin.review_created",
            pr_url=pr_url,
            pr_number=data.get("pr_number"),
            status=data.get("status"),
            commit_sha=data.get("commit_sha"),
        )
        return data

    async def get_pr_review(self, pr_url: str, commit_sha: str | None = None) -> dict:
        """Fetch the latest Devin Review status for a PR."""
        params: dict = {"pr_url": pr_url}
        if commit_sha:
            params["commit_sha"] = commit_sha
        resp = await self._request("GET", self._pr_reviews_path(), params=params)
        return resp.json()

    async def check_connectivity(self) -> bool:
        if not self.configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # /v3/self validates service-user credentials
                root = self.api_base.rsplit("/v", 1)[0]
                resp = await client.get(f"{root}/v3/self", headers=self._headers())
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
