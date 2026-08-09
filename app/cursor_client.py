"""Async client for the Cursor Cloud Agents REST API v1.

Docs: https://cursor.com/docs/cloud-agent/api/endpoints
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.logging_conf import log_event
from app.models import TaskStatus

logger = logging.getLogger("app.cursor")

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

REVIEW_PROMPT_TEMPLATE = """Review the pull request at {pr_url} in repository {repository_url}.

Perform a thorough code review:
- Identify bugs, regressions, missing tests, and security issues.
- Check that the change matches the PR description and linked issue (if any).
- Leave a clear verdict: approve with notes, or request changes with concrete fixes.
- Summarize findings at the end. Do not open a new pull request.
"""

# Cursor run status -> internal TaskStatus
_RUN_STATUS_MAP = {
    "CREATING": TaskStatus.QUEUED,
    "RUNNING": TaskStatus.RUNNING,
    "FINISHED": TaskStatus.COMPLETED,
    "ERROR": TaskStatus.FAILED,
    "CANCELLED": TaskStatus.FAILED,
    "CANCELED": TaskStatus.FAILED,
    "EXPIRED": TaskStatus.FAILED,
}


def build_prompt(repository_url: str, issue_number: int, issue_title: str, issue_body: str) -> str:
    return PROMPT_TEMPLATE.format(
        repository_url=repository_url,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body or "(no description provided)",
    )


def build_review_prompt(repository_url: str, pr_url: str) -> str:
    return REVIEW_PROMPT_TEMPLATE.format(repository_url=repository_url, pr_url=pr_url)


def map_run_status(
    status: str | None,
    has_pull_request: bool,
    *,
    pr_merged: bool = False,
) -> str:
    """Map Cursor run status to an internal TaskStatus."""
    value = (status or "").upper()
    if pr_merged:
        return TaskStatus.COMPLETED
    if value in ("ERROR", "EXPIRED", "CANCELLED", "CANCELED"):
        return TaskStatus.FAILED
    if value == "FINISHED":
        return TaskStatus.COMPLETED
    if value in _RUN_STATUS_MAP:
        mapped = _RUN_STATUS_MAP[value]
        # Still creating/running but PR already exists — keep running until FINISHED
        # unless merge already landed.
        if mapped == TaskStatus.QUEUED and has_pull_request:
            return TaskStatus.RUNNING
        return mapped
    return TaskStatus.RUNNING if not has_pull_request else TaskStatus.RUNNING


def extract_pull_request_url(run_or_agent: dict) -> str | None:
    """Pull PR URL from a run (or agent+run) payload."""
    git = run_or_agent.get("git") or {}
    branches = git.get("branches") or []
    for item in branches:
        if isinstance(item, dict):
            url = item.get("prUrl") or item.get("pr_url")
            if url:
                return url
    # Some payloads nest under agent
    for key in ("run", "latestRun", "latest_run"):
        nested = run_or_agent.get(key)
        if isinstance(nested, dict):
            found = extract_pull_request_url(nested)
            if found:
                return found
    return None


def extract_branch_name(run_data: dict) -> str | None:
    git = run_data.get("git") or {}
    for item in git.get("branches") or []:
        if isinstance(item, dict) and item.get("branch"):
            return item["branch"]
    return None


def agent_web_url(agent_id: str | None, agent_url: str | None = None) -> str | None:
    if agent_url:
        return agent_url
    if agent_id:
        return f"https://cursor.com/agents/{agent_id}"
    return None


class CursorClient:
    """Async client for creating and monitoring Cursor Cloud Agents."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        name_prefix: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.cursor_api_key
        self.api_base = (api_base or settings.cursor_api_base).rstrip("/")
        self.model = model if model is not None else settings.cursor_model
        self.name_prefix = (
            name_prefix if name_prefix is not None else settings.cursor_name_prefix
        )
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

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
                await asyncio.sleep(2**attempt)
        raise last_exc  # type: ignore[misc]

    def _default_starting_ref(self) -> str:
        return get_settings().cursor_starting_ref or "main"

    async def create_agent(
        self,
        prompt: str,
        *,
        repository_url: str,
        name: str | None = None,
        starting_ref: str | None = None,
        auto_create_pr: bool = True,
        pr_url: str | None = None,
    ) -> dict:
        """Create a Cloud Agent and return {agent_id, run_id, url, raw}."""
        repo_entry: dict[str, Any] = {"url": repository_url}
        if pr_url:
            repo_entry["prUrl"] = pr_url
        else:
            repo_entry["startingRef"] = starting_ref or self._default_starting_ref()

        payload: dict[str, Any] = {
            "prompt": {"text": prompt},
            "repos": [repo_entry],
            "autoCreatePR": auto_create_pr,
            "skipReviewerRequest": True,
        }
        if name:
            display = name
            if self.name_prefix and not display.startswith(self.name_prefix):
                display = f"{self.name_prefix}: {display}"
            payload["name"] = display[:100]
        if self.model:
            payload["model"] = {"id": self.model}

        resp = await self._request("POST", "/v1/agents", json=payload)
        data = resp.json()
        agent = data.get("agent") or {}
        run = data.get("run") or {}
        agent_id = agent.get("id")
        run_id = run.get("id") or agent.get("latestRunId")
        url = agent.get("url") or agent_web_url(agent_id)
        log_event(
            logger,
            logging.INFO,
            "cursor.agent_created",
            agent_id=agent_id,
            run_id=run_id,
            url=url,
            auto_create_pr=auto_create_pr,
        )
        return {
            "agent_id": agent_id,
            "run_id": run_id,
            "url": url,
            "agent": agent,
            "run": run,
            "raw": data,
        }

    async def create_review_agent(
        self,
        *,
        repository_url: str,
        pr_url: str,
        name: str | None = None,
    ) -> dict:
        prompt = build_review_prompt(repository_url, pr_url)
        return await self.create_agent(
            prompt,
            repository_url=repository_url,
            name=name or f"Review {pr_url}",
            auto_create_pr=False,
            pr_url=pr_url,
        )

    async def get_agent(self, agent_id: str) -> dict:
        resp = await self._request("GET", f"/v1/agents/{agent_id}")
        return resp.json()

    async def get_run(self, agent_id: str, run_id: str) -> dict:
        resp = await self._request("GET", f"/v1/agents/{agent_id}/runs/{run_id}")
        return resp.json()

    async def get_latest_run(self, agent_id: str) -> dict | None:
        agent = await self.get_agent(agent_id)
        run_id = agent.get("latestRunId") or agent.get("latest_run_id")
        if not run_id:
            # Fall back to list runs
            try:
                resp = await self._request("GET", f"/v1/agents/{agent_id}/runs", params={"limit": 1})
                items = resp.json().get("items") or []
                if items:
                    return items[0]
            except httpx.HTTPError:
                return None
            return None
        run = await self.get_run(agent_id, run_id)
        run["_agent"] = agent
        return run

    async def get_agent_status(self, agent_id: str, run_id: str | None = None) -> tuple[str, str | None, dict]:
        """Return (internal_status, pull_request_url, run_data)."""
        if run_id:
            run = await self.get_run(agent_id, run_id)
        else:
            run = await self.get_latest_run(agent_id)
            if run is None:
                return TaskStatus.QUEUED, None, {}
        pr_url = extract_pull_request_url(run)
        status = map_run_status(run.get("status"), bool(pr_url))
        return status, pr_url, run

    async def get_run_summary(self, agent_id: str, run_id: str | None = None) -> str | None:
        if run_id:
            run = await self.get_run(agent_id, run_id)
        else:
            run = await self.get_latest_run(agent_id)
        if not run:
            return None
        result = run.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
        return None

    @staticmethod
    def run_duration_seconds(run_data: dict) -> float | None:
        ms = run_data.get("durationMs")
        if ms is None:
            return None
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def map_review_status(status: str | None) -> str:
        return map_run_status(status, has_pull_request=True)

    async def check_connectivity(self) -> bool:
        if not self.configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{self.api_base}/v1/me",
                    headers=self._headers(),
                )
                if resp.status_code == 200:
                    return True
                # Some accounts expose models but not /v1/me
                resp = await client.get(
                    f"{self.api_base}/v1/models",
                    headers=self._headers(),
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
