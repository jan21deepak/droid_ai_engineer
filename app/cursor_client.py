"""Cursor Cloud Agents client built on the official Python Cursor SDK.

Uses ``cursor_sdk.Agent`` / ``CloudAgentOptions`` (not raw REST). Sync SDK
calls run in a thread so FastAPI's async worker can orchestrate many agents.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
from cursor_sdk import (
    Agent,
    AgentOptions,
    CloudAgentOptions,
    CloudEnvironment,
    CloudRepository,
    Cursor,
    CursorAgentError,
    close_default_client,
)

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
- Run the project's test suite (or the closest relevant subset if the full suite is impractical).
- Fix any failures introduced by your changes.
- Push your work to a new branch on THIS repository ({repository_url}).
- Do NOT open a pull request against an upstream / parent repository. If this repo is a fork, keep all branches and PRs inside the fork only (base = this fork's default branch). Prefer leaving PR creation to automation when unsure.
- Summarize the completed work and the branch name at the end.
"""

REVIEW_PROMPT_TEMPLATE = """Review the pull request at {pr_url} in repository {repository_url}.

Perform a thorough code review focused on production readiness for a fintech / core-banking style change:
- Identify bugs, regressions, missing tests, and security issues.
- Check that the change matches the PR description and linked issue (if any).
- Leave a clear verdict: approve with notes, or request changes with concrete fixes.
- Summarize findings at the end. Do not open a new pull request.
"""

FOLLOW_UP_PROMPT_TEMPLATE = """Continue work on this agent session.

Follow-up instruction:
{instruction}

Requirements:
- Keep the existing branch / PR when possible.
- Run relevant tests after changes.
- Summarize what you changed.
"""

# SDK run statuses are lowercase; accept REST-era uppercase too.
_RUN_STATUS_MAP = {
    "CREATING": TaskStatus.QUEUED,
    "RUNNING": TaskStatus.RUNNING,
    "FINISHED": TaskStatus.COMPLETED,
    "ERROR": TaskStatus.FAILED,
    "CANCELLED": TaskStatus.FAILED,
    "CANCELED": TaskStatus.FAILED,
    "EXPIRED": TaskStatus.FAILED,
    "creating": TaskStatus.QUEUED,
    "running": TaskStatus.RUNNING,
    "finished": TaskStatus.COMPLETED,
    "error": TaskStatus.FAILED,
    "cancelled": TaskStatus.FAILED,
    "canceled": TaskStatus.FAILED,
    "expired": TaskStatus.FAILED,
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


def build_follow_up_prompt(instruction: str) -> str:
    return FOLLOW_UP_PROMPT_TEMPLATE.format(instruction=instruction.strip() or "Continue.")


def map_run_status(
    status: str | None,
    has_pull_request: bool,
    *,
    pr_merged: bool = False,
) -> str:
    """Map Cursor SDK / API run status to an internal TaskStatus."""
    raw = status or ""
    value = raw.upper()
    if pr_merged:
        return TaskStatus.COMPLETED
    if value in ("ERROR", "EXPIRED", "CANCELLED", "CANCELED"):
        return TaskStatus.FAILED
    if value == "FINISHED":
        return TaskStatus.COMPLETED
    mapped = _RUN_STATUS_MAP.get(raw) or _RUN_STATUS_MAP.get(value)
    if mapped is not None:
        if mapped == TaskStatus.QUEUED and has_pull_request:
            return TaskStatus.RUNNING
        return mapped
    return TaskStatus.RUNNING


def extract_pull_request_url(run_or_agent: dict) -> str | None:
    """Pull PR URL from a normalized run dict (SDK git snapshot)."""
    git = run_or_agent.get("git") or {}
    branches = git.get("branches") or []
    for item in branches:
        if isinstance(item, dict):
            url = item.get("prUrl") or item.get("pr_url")
            if url:
                return url
    for key in ("run", "latestRun", "latest_run"):
        nested = run_or_agent.get(key)
        if isinstance(nested, dict):
            found = extract_pull_request_url(nested)
            if found:
                return found
    return None


def extract_agent_branch(run_or_agent: dict) -> str | None:
    """Return the first agent branch name from a normalized run dict."""
    git = run_or_agent.get("git") or {}
    branches = git.get("branches") or []
    for item in branches:
        if isinstance(item, dict):
            branch = item.get("branch") or ""
            if branch:
                return branch
    for key in ("run", "latestRun", "latest_run"):
        nested = run_or_agent.get(key)
        if isinstance(nested, dict):
            found = extract_agent_branch(nested)
            if found:
                return found
    # Fallback: Cursor often writes the branch into the run result when PR create fails.
    result = run_or_agent.get("result") or ""
    if isinstance(result, str) and result:
        match = re.search(r"\bBranch\s+(cursor/[A-Za-z0-9._/-]+)\b", result)
        if match:
            return match.group(1)
        # Manual compare links: .../pull/new/cursor/branch-name
        match = re.search(
            r"github\.com/[^/\s]+/[^/\s]+/pull/new/(cursor/[A-Za-z0-9._/-]+)",
            result,
        )
        if match:
            return match.group(1)
        match = re.search(r"\b(cursor/[A-Za-z0-9._/-]+)\b", result)
        if match:
            return match.group(1)
    return None


def agent_web_url(agent_id: str | None, agent_url: str | None = None) -> str | None:
    if agent_url:
        return agent_url
    if agent_id:
        return f"https://cursor.com/agents/{agent_id}"
    return None


def _run_to_dict(run: Any) -> dict:
    """Normalize an SDK Run / snapshot into the dict shape the worker expects."""
    git_obj = getattr(run, "git", None)
    branches: list[dict] = []
    if git_obj is not None:
        for branch in getattr(git_obj, "branches", ()) or ():
            branches.append(
                {
                    "repoUrl": getattr(branch, "repo_url", "") or "",
                    "branch": getattr(branch, "branch", "") or "",
                    "prUrl": getattr(branch, "pr_url", "") or "",
                }
            )
    status = getattr(run, "status", None) or "running"
    duration_ms = getattr(run, "duration_ms", None) or 0
    return {
        "id": getattr(run, "id", None) or getattr(run, "run_id", None),
        "agentId": getattr(run, "agent_id", None),
        "status": status,
        "result": getattr(run, "result", None) or "",
        "durationMs": duration_ms,
        "git": {"branches": branches},
        "createdAt": getattr(run, "created_at", None),
    }


class CursorClient:
    """Async facade over the sync Cursor SDK cloud agent APIs."""

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
        # Kept for health/docs compatibility; SDK owns transport.
        self.api_base = (api_base or settings.cursor_api_base).rstrip("/")
        self.model = (model if model is not None else settings.cursor_model) or "composer-2.5"
        self.name_prefix = (
            name_prefix if name_prefix is not None else settings.cursor_name_prefix
        )
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _display_name(self, name: str | None) -> str | None:
        if not name:
            return None
        display = name
        if self.name_prefix and not display.startswith(self.name_prefix):
            display = f"{self.name_prefix}: {display}"
        return display[:100]

    def _cloud_options(
        self,
        *,
        repository_url: str,
        starting_ref: str | None,
        auto_create_pr: bool,
        pr_url: str | None,
        environment: str | None,
    ) -> CloudAgentOptions:
        """Build CloudAgentOptions for the target OSS repo.

        Named Cursor environments (``env.name``) already bind a repository +
        install/snapshot config. The Cloud Agents API treats a named cloud
        environment as mutually exclusive with an explicit ``repos`` list, so
        when ``environment`` is set we select that env and omit ``repos``.

        PR review agents still need ``repos[].pr_url`` to attach to the pull
        request branch, so reviews keep the ``repos`` path even when a named
        environment exists for the repo.
        """
        env_name = (environment or "").strip() or None
        if env_name and not pr_url:
            return CloudAgentOptions(
                env=CloudEnvironment(type="cloud", name=env_name),
                auto_create_pr=auto_create_pr,
                skip_reviewer_request=True,
            )

        repo = CloudRepository(
            url=repository_url,
            starting_ref=None if pr_url else (starting_ref or self._default_starting_ref()),
            pr_url=pr_url,
        )
        return CloudAgentOptions(
            repos=[repo],
            auto_create_pr=auto_create_pr,
            skip_reviewer_request=True,
        )

    def _default_starting_ref(self) -> str:
        return get_settings().cursor_starting_ref or "main"

    def _create_agent_sync(
        self,
        prompt: str,
        *,
        repository_url: str,
        name: str | None,
        starting_ref: str | None,
        auto_create_pr: bool,
        pr_url: str | None,
        environment: str | None,
    ) -> dict:
        cloud = self._cloud_options(
            repository_url=repository_url,
            starting_ref=starting_ref,
            auto_create_pr=auto_create_pr,
            pr_url=pr_url,
            environment=environment,
        )
        agent = Agent.create(
            model=self.model,
            api_key=self.api_key,
            name=self._display_name(name),
            cloud=cloud,
        )
        try:
            run = agent.send(prompt)
            agent_id = agent.agent_id
            run_id = run.id
            url = agent_web_url(agent_id)
            env_name = (environment or "").strip() or None
            log_event(
                logger,
                logging.INFO,
                "cursor.agent_created",
                agent_id=agent_id,
                run_id=run_id,
                url=url,
                auto_create_pr=auto_create_pr,
                environment=env_name,
                repository_url=repository_url,
                sdk="cursor-sdk",
            )
            return {
                "agent_id": agent_id,
                "run_id": run_id,
                "url": url,
                "environment": env_name,
                "agent": {"id": agent_id, "url": url},
                "run": _run_to_dict(run),
                "raw": {"sdk": True},
            }
        finally:
            try:
                agent.close()
            except Exception:
                pass

    async def create_agent(
        self,
        prompt: str,
        *,
        repository_url: str,
        name: str | None = None,
        starting_ref: str | None = None,
        auto_create_pr: bool = True,
        pr_url: str | None = None,
        environment: str | None = None,
    ) -> dict:
        """Create a Cloud Agent via the SDK and enqueue the first run (no wait).

        Pass ``environment`` (Cursor Cloud Agents dashboard env name) so the
        agent boots the OSS repo's configured environment rather than a bare
        clone / the forge app's own environment.
        """
        try:
            return await asyncio.to_thread(
                self._create_agent_sync,
                prompt,
                repository_url=repository_url,
                name=name,
                starting_ref=starting_ref,
                auto_create_pr=auto_create_pr,
                pr_url=pr_url,
                environment=environment,
            )
        except CursorAgentError as exc:
            log_event(
                logger,
                logging.ERROR,
                "cursor.agent_create_failed",
                error=str(exc),
                retryable=getattr(exc, "is_retryable", None),
                environment=(environment or "").strip() or None,
                repository_url=repository_url,
            )
            raise

    async def create_review_agent(
        self,
        *,
        repository_url: str,
        pr_url: str,
        name: str | None = None,
        environment: str | None = None,
        starting_ref: str | None = None,
    ) -> dict:
        prompt = build_review_prompt(repository_url, pr_url)
        return await self.create_agent(
            prompt,
            repository_url=repository_url,
            name=name or f"Review {pr_url}",
            auto_create_pr=False,
            pr_url=pr_url,
            # Reviews keep repos+pr_url for branch targeting; environment is
            # accepted for API symmetry but not applied while pr_url is set.
            environment=environment,
            starting_ref=starting_ref,
        )

    def _get_run_sync(self, agent_id: str, run_id: str):
        return Agent.get_run(
            run_id,
            {"runtime": "cloud", "agentId": agent_id, "agent_id": agent_id},
        )

    def _latest_run_sync(self, agent_id: str):
        result = Agent.list_runs(agent_id, {"runtime": "cloud", "limit": 1})
        items = getattr(result, "items", None) or []
        return items[0] if items else None

    def _get_agent_status_sync(
        self, agent_id: str, run_id: str | None
    ) -> tuple[str, str | None, dict]:
        if run_id:
            run = self._get_run_sync(agent_id, run_id)
        else:
            run = self._latest_run_sync(agent_id)
            if run is None:
                return TaskStatus.QUEUED, None, {}
        data = _run_to_dict(run)
        pr_url = extract_pull_request_url(data)
        status = map_run_status(data.get("status"), bool(pr_url))
        return status, pr_url, data

    async def get_agent_status(
        self, agent_id: str, run_id: str | None = None
    ) -> tuple[str, str | None, dict]:
        """Return (internal_status, pull_request_url, run_data)."""
        return await asyncio.to_thread(self._get_agent_status_sync, agent_id, run_id)

    async def get_run_summary(self, agent_id: str, run_id: str | None = None) -> str | None:
        _, _, run = await self.get_agent_status(agent_id, run_id)
        result = run.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
        return None

    def _follow_up_sync(self, agent_id: str, prompt: str) -> dict:
        agent = Agent.resume(agent_id, AgentOptions(api_key=self.api_key))
        try:
            run = agent.send(prompt)
            run_id = run.id
            log_event(
                logger,
                logging.INFO,
                "cursor.follow_up_sent",
                agent_id=agent_id,
                run_id=run_id,
            )
            return {
                "agent_id": agent_id,
                "run_id": run_id,
                "url": agent_web_url(agent_id),
                "run": _run_to_dict(run),
            }
        finally:
            try:
                agent.close()
            except Exception:
                pass

    async def send_follow_up(self, agent_id: str, instruction: str) -> dict:
        """Send a follow-up prompt to an existing cloud agent (live-extend hook)."""
        prompt = build_follow_up_prompt(instruction)
        try:
            return await asyncio.to_thread(self._follow_up_sync, agent_id, prompt)
        except CursorAgentError as exc:
            log_event(
                logger,
                logging.ERROR,
                "cursor.follow_up_failed",
                agent_id=agent_id,
                error=str(exc),
            )
            raise

    @staticmethod
    def run_duration_seconds(run_data: dict) -> float | None:
        ms = run_data.get("durationMs")
        if ms is None:
            return None
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value / 1000.0

    @staticmethod
    def map_review_status(status: str | None) -> str:
        return map_run_status(status, has_pull_request=True)

    def _check_connectivity_sync(self) -> bool:
        try:
            Cursor.models.list(api_key=self.api_key)
            return True
        except Exception:
            try:
                Cursor.me(api_key=self.api_key)
                return True
            except Exception:
                return False

    async def check_connectivity(self) -> bool:
        if not self.configured:
            return False
        return await asyncio.to_thread(self._check_connectivity_sync)

    def _list_environments_sync(self, *, limit: int = 50) -> list[dict]:
        """Discover named cloud environments from recent Cloud Agents.

        Cursor does not expose a public list-environments endpoint yet, so we
        collect distinct ``env.name`` values from agents the API key can see.
        """
        if not self.api_key:
            return []

        names: dict[str, dict] = {}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(
                    f"{self.api_base.rstrip('/')}/v1/agents",
                    params={"limit": max(1, min(limit, 100))},
                    auth=(self.api_key, ""),
                )
                resp.raise_for_status()
                payload = resp.json() or {}
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "cursor.environments_list_failed",
                error=str(exc),
            )
            return []

        items = payload.get("items") or payload.get("agents") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            env = item.get("env") or {}
            name = (env.get("name") or "").strip() if isinstance(env, dict) else ""
            if not name:
                continue
            repos = item.get("repos") or []
            repo_url = ""
            if repos and isinstance(repos[0], dict):
                repo_url = repos[0].get("url") or ""
            source = item.get("source") or {}
            if not repo_url and isinstance(source, dict):
                repo_url = source.get("repository") or ""
            entry = names.setdefault(
                name,
                {"name": name, "repositories": set(), "source": "cursor"},
            )
            if repo_url:
                entry["repositories"].add(repo_url)

        result = []
        for name in sorted(names, key=str.lower):
            entry = names[name]
            result.append(
                {
                    "name": name,
                    "repositories": sorted(entry["repositories"]),
                    "source": "cursor",
                }
            )
        return result

    async def list_environments(self, *, limit: int = 50) -> list[dict]:
        """Return named Cursor Cloud Agent environments visible to this API key."""
        if not self.configured:
            return []
        return await asyncio.to_thread(self._list_environments_sync, limit=limit)

    @staticmethod
    def shutdown() -> None:
        """Dispose the SDK default client (call on app shutdown)."""
        try:
            close_default_client()
        except Exception:
            pass
