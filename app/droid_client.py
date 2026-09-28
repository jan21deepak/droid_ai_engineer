"""Droid (Factory) client built on the official Python SDK (``droid-sdk``).

Cursor Forge delegated implementation to remote Cursor Cloud Agents. Droid
Forge instead runs **local Droid sessions**: for every issue the client
prepares a workspace clone of the target repository (with an authenticated
push remote), opens a Droid session inside that clone, and streams the fix
turn in a background asyncio task. The run registry keeps shutdown orderly
and makes post-restart recovery possible via ``Session.resume``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from droid_sdk import Autonomy, Session, SessionConfig, list_models

from app.config import get_settings
from app.logging_conf import log_event
from app.models import TaskStatus
from app.repos import parse_repository_ref

logger = logging.getLogger("app.droid")

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

FIX_PROMPT_TEMPLATE = """You are working in a local clone of the repository: {repository_url}
(checkout of ref "{starting_ref}"). The "origin" remote is already authenticated for pushes.

Resolve the following GitHub issue (#{issue_number}):

Title: {issue_title}

Description:
{issue_body}

Requirements:
- Make only the requested changes described in the issue.
- Create a new git branch for your work.
- Run the project's test suite (or the closest relevant subset if the full suite is impractical) and fix any failures introduced by your changes.
- Push your branch to the "origin" remote. Do NOT open a pull request; automation opens a same-repo pull request for you.
- Do NOT rebase onto or push to any upstream / parent repository. If this repo is a fork, keep all branches inside the fork only.
- End your final response with a line of the exact form `BRANCH: <your-branch-name>` followed by a short summary of the completed work.
"""

REVIEW_PROMPT_TEMPLATE = """Review the code changes on the currently checked-out branch of the repository: {repository_url}
Pull request: {pr_url}
The changes are relative to the base ref "{base_ref}" (use `git diff` to inspect them).

Perform a thorough code review focused on production readiness:
- Identify bugs, regressions, missing tests, and security issues.
- Check that the change is coherent and complete on its own.
- Leave a clear verdict: approve with notes, or request changes with concrete fixes.
- End your response with exactly one line: `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES`.
- Do not modify files, push branches, or open pull requests. Review only.
"""

FOLLOW_UP_PROMPT_TEMPLATE = """Continue work on this agent session.

Follow-up instruction:
{instruction}

Requirements:
- Keep the existing branch / pull request when possible.
- Push your updated branch to "origin" when done.
- Run relevant tests after changes.
- End your final response with a line of the exact form `BRANCH: <your-branch-name>` and summarize what you changed.
"""

RECOVERY_PROMPT_TEMPLATE = """The automation service restarted while you were working, so this session is
being resumed. Continue the task from where you left off.

- If the work is already complete, restate your final summary.
- Make sure your branch is pushed to the "origin" remote.
- End your final response with a line of the exact form `BRANCH: <your-branch-name>` and a short summary.
"""

_BRANCH_RE = re.compile(r"^\s*BRANCH:\s*([A-Za-z0-9._/-]+)\s*$", re.MULTILINE)
_VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", re.IGNORECASE)

GIT_TIMEOUT_SECONDS = 300.0
SETUP_TIMEOUT_SECONDS = 900.0


class DroidRunError(Exception):
    """Raised when a Droid session cannot be started or resumed."""


class DroidSessionBusyError(DroidRunError):
    """Raised when the session already has an active turn in flight."""


def build_fix_prompt(
    repository_url: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    starting_ref: str,
) -> str:
    return FIX_PROMPT_TEMPLATE.format(
        repository_url=repository_url,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body or "(no description provided)",
        starting_ref=starting_ref or "the repository default branch",
    )


def build_review_prompt(
    repository_url: str, pr_url: str, base_ref: str
) -> str:
    return REVIEW_PROMPT_TEMPLATE.format(
        repository_url=repository_url,
        pr_url=pr_url,
        base_ref=base_ref or "the repository default branch",
    )


def build_follow_up_prompt(instruction: str) -> str:
    return FOLLOW_UP_PROMPT_TEMPLATE.format(
        instruction=instruction.strip() or "Continue."
    )


def extract_branch_name(text: str | None) -> str | None:
    """Pull the agent-reported branch name from a turn result."""
    if not text:
        return None
    match = _BRANCH_RE.search(text)
    return match.group(1) if match else None


def extract_verdict(text: str | None) -> str | None:
    """Pull the review verdict line from a review turn result."""
    if not text:
        return None
    match = _VERDICT_RE.search(text)
    return match.group(1).upper() if match else None


def map_outcome_status(outcome: dict) -> str:
    """Map a Droid turn outcome to an internal TaskStatus."""
    return TaskStatus.COMPLETED if outcome.get("success") else TaskStatus.FAILED


def workspace_root() -> Path:
    return Path(get_settings().workspace_root or "./data/workspace").expanduser().resolve()


def task_workspace_dir(repository: str, task_id: int) -> Path:
    full_name = parse_repository_ref(repository) or repository
    owner, _, repo = full_name.partition("/")
    return workspace_root() / owner / repo / f"task-{task_id}"


def review_workspace_dir(repository: str, review_id: int) -> Path:
    full_name = parse_repository_ref(repository) or repository
    owner, _, repo = full_name.partition("/")
    return workspace_root() / owner / repo / f"review-{review_id}"


async def _git(args: list[str], *, cwd: Path | str | None = None, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    """Run a git command, returning stdout. Raises DroidRunError on failure."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=GIT_ENV,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise DroidRunError(f"git {' '.join(args[:3])} … timed out after {timeout}s") from None
    if proc.returncode != 0:
        detail = (err or b"").decode(errors="replace").strip()[:500]
        raise DroidRunError(f"git {' '.join(args[:4])} failed (exit {proc.returncode}): {detail}")
    return out.decode(errors="replace").strip()


def _remote_url(repository: str, token: str | None) -> str:
    full_name = parse_repository_ref(repository) or repository
    if token:
        return f"https://x-access-token:{token}@github.com/{full_name}.git"
    return f"https://github.com/{full_name}.git"


class DroidClient:
    """Async facade over local Droid sessions (droid-sdk)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else (settings.factory_api_key or None)
        self.allow_cli_auth = settings.droid_allow_cli_auth
        self.model = (model if model is not None else settings.droid_model) or "auto"
        self.timeout_seconds = float(settings.droid_turn_timeout_seconds or 3600.0)
        # Background asyncio tasks: session_id / "launch:<id>" -> Task
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def configured(self) -> bool:
        """True when a Factory API key is set, or when CLI auth is allowed and
        the `droid` CLI is installed (the SDK then uses its login credentials).
        """
        if self.api_key:
            return True
        return bool(self.allow_cli_auth and shutil.which("droid"))

    @property
    def active_sessions(self) -> set[str]:
        return {
            key
            for key, task in self._tasks.items()
            if not key.startswith("launch:") and not task.done()
        }

    # ---- registry -------------------------------------------------------

    def track(self, key: str, task: asyncio.Task) -> asyncio.Task:
        existing = self._tasks.get(key)
        if existing and not existing.done():
            raise DroidSessionBusyError(f"run {key} is already in flight")
        self._tasks[key] = task
        task.add_done_callback(lambda _t: self._tasks.pop(key, None))
        return task

    def session_busy(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return bool(task and not task.done())

    async def shutdown(self) -> None:
        """Cancel in-flight runs; Droid sessions save state and stay resumable."""
        for key, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    # ---- settings / discovery -------------------------------------------

    def autonomy(self, *, review: bool = False) -> Autonomy:
        if review:
            return Autonomy.OFF
        level = (get_settings().droid_autonomy or "high").strip().lower()
        return {
            "off": Autonomy.OFF,
            "low": Autonomy.LOW,
            "medium": Autonomy.MEDIUM,
            "high": Autonomy.HIGH,
        }.get(level, Autonomy.HIGH)

    def _session_config(self, *, review: bool) -> SessionConfig:
        return SessionConfig(
            autonomy=self.autonomy(review=review),
            # Headless runs must never hang waiting for a human answer.
            auto_reject_permission_requests=True,
        )

    async def check_connectivity(self) -> bool:
        if not self.configured:
            return False
        try:
            await list_models(api_key=self.api_key)
            return True
        except Exception:
            return False

    async def list_available_models(self) -> list[dict]:
        if not self.configured:
            return []
        try:
            models = await list_models(api_key=self.api_key)
        except Exception as exc:
            log_event(logger, logging.WARNING, "droid.models_list_failed", error=str(exc))
            return []
        result = []
        for item in models:
            disabled = bool(getattr(item, "disabled", False))
            if disabled:
                continue
            result.append(
                {
                    "id": getattr(item, "id", ""),
                    "display_name": getattr(item, "display_name", "") or getattr(item, "id", ""),
                    "provider": getattr(item, "model_provider", ""),
                }
            )
        return result

    # ---- workspaces ------------------------------------------------------

    async def _run_setup_command(self, ws: Path, command: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=str(ws),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=GIT_ENV,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=SETUP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            log_event(
                logger,
                logging.WARNING,
                "droid.setup_command_timeout",
                workspace=str(ws),
            )
            return
        if proc.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "droid.setup_command_failed",
                workspace=str(ws),
                exit_code=proc.returncode,
                output=(out or b"").decode(errors="replace")[-500:],
            )

    async def prepare_fix_workspace(
        self,
        *,
        repository: str,
        task_id: int,
        starting_ref: str | None,
        setup_command: str = "",
    ) -> Path:
        """Clone/refresh the per-task workspace with an authenticated remote."""
        settings = get_settings()
        ws = task_workspace_dir(repository, task_id)
        remote = _remote_url(repository, settings.github_token or None)
        ws.parent.mkdir(parents=True, exist_ok=True)

        if (ws / ".git").exists():
            # Reuse (restart recovery for the same task): keep agent state, only
            # refresh the remote URL so a rotated token still works.
            await _git(["remote", "set-url", "origin", remote], cwd=ws)
        else:
            ref = (starting_ref or "").strip()
            try:
                if ref:
                    await _git(["clone", "--depth", "1", "--branch", ref, remote, str(ws)])
                else:
                    await _git(["clone", "--depth", "1", remote, str(ws)])
            except DroidRunError:
                if not ws.exists() or not (ws / ".git").exists():
                    raise
                # Clone partially succeeded (e.g. unknown ref); keep what we got.
            await _git(["config", "user.name", "Droid Forge"], cwd=ws)
            await _git(["config", "user.email", "droid-forge@users.noreply.github.com"], cwd=ws)

        if (setup_command or "").strip():
            await self._run_setup_command(ws, setup_command.strip())
        return ws

    async def prepare_review_workspace(
        self,
        *,
        repository: str,
        review_id: int,
        pr_number: int,
    ) -> Path:
        """Clone the repo and check out the pull request head branch."""
        settings = get_settings()
        ws = review_workspace_dir(repository, review_id)
        remote = _remote_url(repository, settings.github_token or None)
        ws.parent.mkdir(parents=True, exist_ok=True)

        if not (ws / ".git").exists():
            await _git(["clone", "--depth", "1", remote, str(ws)])
        else:
            await _git(["remote", "set-url", "origin", remote], cwd=ws)

        branch = f"pr-{pr_number}"
        await _git(
            ["fetch", "--depth", "1", "--force", "origin", f"pull/{pr_number}/head:{branch}"],
            cwd=ws,
        )
        await _git(["checkout", "-q", "--force", branch], cwd=ws)
        return ws

    # ---- branch helpers ---------------------------------------------------

    async def current_branch(self, repository: str, task_id: int) -> str | None:
        try:
            branch = await _git(
                ["rev-parse", "--abbrev-ref", "HEAD"], cwd=task_workspace_dir(repository, task_id)
            )
        except DroidRunError:
            return None
        return branch or None

    async def ensure_branch_pushed(self, repository: str, task_id: int, branch: str) -> bool:
        """Make sure the agent branch exists on origin; push it ourselves if not."""
        ws = task_workspace_dir(repository, task_id)
        try:
            remote_refs = await _git(["ls-remote", "origin", f"refs/heads/{branch}"], cwd=ws)
        except DroidRunError:
            return False
        if remote_refs:
            return True
        try:
            await _git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=ws)
            return True
        except DroidRunError as exc:
            log_event(
                logger,
                logging.WARNING,
                "droid.branch_push_failed",
                task_id=task_id,
                branch=branch,
                error=str(exc),
            )
            return False

    # ---- session runs ------------------------------------------------------

    async def _run_turn(
        self,
        session: Session,
        prompt: str,
        *,
        kind: str,
        on_complete: Callable[[dict], Any] | None,
    ) -> dict:
        session_id = session.id
        outcome: dict = {"session_id": session_id, "kind": kind}
        events = 0
        try:
            async with session.stream(prompt, timeout=self.timeout_seconds) as stream:
                async for message in stream:
                    events += 1
                    if events % 25 == 0:
                        log_event(
                            logger,
                            logging.DEBUG,
                            "droid.turn_progress",
                            session_id=session_id,
                            events=events,
                        )
            result = stream.result
            usage = getattr(result, "usage", None)
            tokens = None
            if usage is not None:
                tokens = (getattr(usage, "input_tokens", 0) or 0) + (
                    getattr(usage, "output_tokens", 0) or 0
                )
            duration = getattr(result, "duration", None)
            outcome.update(
                success=bool(result.success),
                subtype=getattr(result, "subtype", ""),
                text=getattr(result, "text", "") or "",
                duration_seconds=duration.total_seconds() if duration else None,
                credits=getattr(usage, "factory_credits", None) if usage else None,
                tokens=tokens or None,
                error=None,
            )
        except asyncio.CancelledError:
            outcome.update(success=False, subtype="cancelled", text="", error="run cancelled")
            raise
        except Exception as exc:
            outcome.update(
                success=False,
                subtype="error_during_execution",
                text="",
                error=str(exc),
            )
        finally:
            try:
                await session.close()
            except Exception:
                pass

        log_event(
            logger,
            logging.INFO if outcome.get("success") else logging.ERROR,
            "droid.turn_finished",
            session_id=session_id,
            kind=kind,
            success=outcome.get("success"),
            subtype=outcome.get("subtype"),
            events=events,
            duration_seconds=outcome.get("duration_seconds"),
        )
        if on_complete is not None:
            try:
                await on_complete(outcome)
            except Exception:
                logger.exception("droid.on_complete_failed session_id=%s", session_id)
        return outcome

    def _spawn_turn(
        self,
        session: Session,
        prompt: str,
        *,
        kind: str,
        on_complete: Callable[[dict], Any] | None,
    ) -> str:
        session_id = session.id
        task = asyncio.create_task(
            self._run_turn(session, prompt, kind=kind, on_complete=on_complete)
        )
        self.track(session_id, task)
        return session_id

    async def start_fix_run(
        self,
        *,
        repository: str,
        repository_url: str,
        task_id: int,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        starting_ref: str | None = None,
        setup_command: str = "",
        model: str | None = None,
        on_complete: Callable[[dict], Any] | None = None,
    ) -> dict:
        """Prepare the workspace, open a Droid session and stream the fix turn.

        Returns ``{"session_id": str, "status": "running"}`` once the session
        is open; the turn itself completes in the background.
        """
        if not self.configured:
            raise DroidRunError("Droid is not configured (FACTORY_API_KEY missing)")
        ws = await self.prepare_fix_workspace(
            repository=repository,
            task_id=task_id,
            starting_ref=starting_ref,
            setup_command=setup_command,
        )
        prompt = build_fix_prompt(
            repository_url, issue_number, issue_title, issue_body, starting_ref or ""
        )
        session = Session(
            cwd=str(ws),
            model=model or self.model,
            config=self._session_config(review=False),
            api_key=self.api_key,
        )
        try:
            await session.open()
        except Exception as exc:
            raise DroidRunError(f"failed to open Droid session: {exc}") from exc
        session_id = self._spawn_turn(
            session, prompt, kind="fix", on_complete=on_complete
        )
        log_event(
            logger,
            logging.INFO,
            "droid.session_started",
            session_id=session_id,
            task_id=task_id,
            repository=repository,
            issue=issue_number,
            workspace=str(ws),
            model=model or self.model,
            sdk="droid-sdk",
        )
        return {"session_id": session_id, "status": "running"}

    async def start_review_run(
        self,
        *,
        repository: str,
        repository_url: str,
        review_id: int,
        pr_number: int,
        pr_url: str,
        base_ref: str | None = None,
        model: str | None = None,
        on_complete: Callable[[dict], Any] | None = None,
    ) -> dict:
        """Open a read-only review session on the PR head branch."""
        if not self.configured:
            raise DroidRunError("Droid is not configured (FACTORY_API_KEY missing)")
        ws = await self.prepare_review_workspace(
            repository=repository, review_id=review_id, pr_number=pr_number
        )
        prompt = build_review_prompt(repository_url, pr_url, base_ref or "")
        session = Session(
            cwd=str(ws),
            model=model or self.model,
            config=self._session_config(review=True),
            api_key=self.api_key,
        )
        try:
            await session.open()
        except Exception as exc:
            raise DroidRunError(f"failed to open Droid review session: {exc}") from exc
        session_id = self._spawn_turn(
            session, prompt, kind="review", on_complete=on_complete
        )
        log_event(
            logger,
            logging.INFO,
            "droid.review_started",
            session_id=session_id,
            review_id=review_id,
            repository=repository,
            pr=pr_number,
            workspace=str(ws),
        )
        return {"session_id": session_id, "status": "running"}

    async def send_follow_up(
        self,
        session_id: str,
        instruction: str,
        *,
        on_complete: Callable[[dict], Any] | None = None,
    ) -> dict:
        """Resume a saved session and run a follow-up turn in the background."""
        if not self.configured:
            raise DroidRunError("Droid is not configured (FACTORY_API_KEY missing)")
        if self.session_busy(session_id):
            raise DroidSessionBusyError("session already has a turn in flight")
        prompt = build_follow_up_prompt(instruction)
        session = Session.resume(session_id, api_key=self.api_key)
        try:
            await session.open()
        except Exception as exc:
            raise DroidRunError(f"failed to resume session {session_id}: {exc}") from exc
        self._spawn_turn(session, prompt, kind="follow_up", on_complete=on_complete)
        log_event(
            logger, logging.INFO, "droid.follow_up_sent", session_id=session_id
        )
        return {"session_id": session_id, "status": "running"}

    async def resume_interrupted(
        self,
        session_id: str,
        *,
        on_complete: Callable[[dict], Any] | None = None,
    ) -> dict:
        """Resume a session orphaned by a service restart and nudge it forward."""
        if not self.configured:
            raise DroidRunError("Droid is not configured (FACTORY_API_KEY missing)")
        if self.session_busy(session_id):
            return {"session_id": session_id, "status": "running"}
        session = Session.resume(session_id, api_key=self.api_key)
        try:
            await session.open()
        except Exception as exc:
            raise DroidRunError(f"failed to resume session {session_id}: {exc}") from exc
        self._spawn_turn(session, RECOVERY_PROMPT_TEMPLATE, kind="recovery", on_complete=on_complete)
        log_event(
            logger, logging.INFO, "droid.session_recovered", session_id=session_id
        )
        return {"session_id": session_id, "status": "running"}


_client: DroidClient | None = None


def get_droid_client() -> DroidClient:
    """Process-wide Droid client (shares the run registry across endpoints)."""
    global _client
    if _client is None:
        _client = DroidClient()
    return _client


def reset_droid_client_for_tests() -> None:
    global _client
    _client = None


__all__ = [
    "DroidClient",
    "DroidRunError",
    "DroidSessionBusyError",
    "build_fix_prompt",
    "build_follow_up_prompt",
    "build_review_prompt",
    "extract_branch_name",
    "extract_verdict",
    "get_droid_client",
    "map_outcome_status",
    "reset_droid_client_for_tests",
    "review_workspace_dir",
    "task_workspace_dir",
    "workspace_root",
]
