"""Droid (Factory) client built on the official Python SDK (``droid-sdk``).

Cursor Forge delegated implementation to remote Cursor Cloud Agents. Droid
Forge instead runs **local Droid sessions**: for every issue the client
prepares a workspace clone of the target repository (with a credential-free
remote), opens a Droid session inside that clone, and streams the fix turn in
a background asyncio task. The run registry keeps shutdown orderly and makes
post-restart recovery possible via ``Session.resume``.

Agent sessions and setup commands run with a clean, allowlisted environment
(see ``app.agent_env``). Only app-owned Git operations receive the GitHub
credential, transiently, and pushes are checked against outward-action policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from droid_sdk import Autonomy, Runtime, Session, SessionConfig, list_models
from droid_sdk.errors import RunTimeoutError

from app.agent_env import agent_environment, close_runtime, git_environment, open_agent_runtime
from app.candidates import WorkspaceState
from app.config import get_settings
from app.logging_conf import log_event
from app.models import TaskStatus
from app.policy import OutwardAction, configured_publication_branch, outward_decision, privileged_setup_allowed
from app.prompts import (
    FIX_PROMPT_TEMPLATE,
    FOLLOW_UP_PROMPT_TEMPLATE,
    RECOVERY_PROMPT_TEMPLATE,
    build_fix_prompt,
    build_follow_up_prompt,
    extract_branch_name,
)
from app.redaction import safe_error
from app.repos import parse_repository_ref
from app.review_policy import (
    ReviewContext,
    ReviewContextIncompleteError,
    assess_diff,
    build_review_prompt,
    review_base_ref,
    review_session_config,
)
from app.run_events import TurnObserver
from app.run_models import USAGE_FIELDS
from app.workspace_config import (
    IDENTITY_VARS,
    ConfigEntry,
    credential_cleanup_commands,
    credential_findings,
    parse_config_list,
    strip_credential_sections,
    usable_identity,
)

logger = logging.getLogger("app.droid")


class SetupStatus:
    """Outcome of the optional per-repository setup command."""

    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled_by_policy"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


GIT_TIMEOUT_SECONDS = 300.0
SETUP_TIMEOUT_SECONDS = 900.0
# After an SDK interrupt request, how long a stopped turn may take to wind down before it is cancelled.
STOP_GRACE_SECONDS = 15.0
INTERRUPT_REQUEST_TIMEOUT_SECONDS = 5.0


class DroidRunError(Exception):
    """Raised when a Droid session cannot be started or resumed."""


class DroidSessionBusyError(DroidRunError):
    """Raised when the session already has an active turn in flight."""


def _usage_categories(usage: object | None) -> dict[str, Any] | None:
    """A terminal result's usage, category by category; ``None`` when the result carried none."""
    if usage is None:
        return None
    return {name: getattr(usage, name, None) for name in USAGE_FIELDS}


def _reported_model(session: Session) -> str | None:
    """The model the opened session reports for itself (may be a router alias such as ``auto``)."""
    try:
        model = session.settings.model
    except Exception:
        return None
    return model if isinstance(model, str) and model else None


def _result_outcome(result: Any) -> dict[str, Any]:
    """Outcome fields from a completed turn's terminal result."""
    usage = getattr(result, "usage", None)
    tokens = None
    if usage is not None:
        tokens = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    duration = getattr(result, "duration", None)
    return {
        "success": bool(result.success),
        "subtype": getattr(result, "subtype", ""),
        "text": getattr(result, "text", "") or "",
        "duration_seconds": duration.total_seconds() if duration else None,
        "credits": getattr(usage, "factory_credits", None) if usage else None,
        "tokens": tokens or None,
        "usage": _usage_categories(usage),
        "error": None,
    }


def map_outcome_status(outcome: dict[str, Any]) -> str:
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


async def _git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
    credential_url: str | None = None,
) -> str:
    """Run an app-owned git command, returning stdout. Raises DroidRunError on failure.

    With ``credential_url`` the configured GitHub token is supplied for that
    URL only, through the child environment; it never appears in ``args``.
    """
    token = (get_settings().github_token or None) if credential_url else None
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=git_environment(token=token, credential_url=credential_url),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise DroidRunError(safe_error(f"git {' '.join(args[:3])} … timed out after {timeout}s")) from None
    if proc.returncode != 0:
        detail = (err or b"").decode(errors="replace").strip()[:500]
        raise DroidRunError(safe_error(f"git {' '.join(args[:4])} failed (exit {proc.returncode}): {detail}"))
    return out.decode(errors="replace").strip()


async def _git_or_none(args: list[str], *, cwd: Path, credential_url: str | None = None) -> str | None:
    """Like ``_git`` but returns ``None`` when the command fails or the workspace is missing."""
    try:
        return await _git(args, cwd=cwd, credential_url=credential_url)
    except (DroidRunError, OSError):
        return None


def _remote_url(repository: str) -> str:
    """Canonical credential-free HTTPS remote for ``repository``."""
    full_name = parse_repository_ref(repository) or repository
    return f"https://github.com/{full_name}.git"


MISSING_IDENTITY_ERROR = (
    "no usable Git identity: configure user.name and user.email (or GIT_AUTHOR_*/GIT_COMMITTER_* "
    "variables) for the service account; Forge never invents or overrides a commit author"
)


async def _require_commit_identity(ws: Path) -> None:
    """Use the configured Git identity unchanged; refuse to run without a usable one.

    ``user.useConfigOnly`` stops Git from guessing an author from the host name.
    """
    for variable in IDENTITY_VARS:
        ident = await _git_or_none(["-c", "user.useConfigOnly=true", "var", variable], cwd=ws)
        if not ident or not usable_identity(ident):
            raise DroidRunError(MISSING_IDENTITY_ERROR)


async def _local_config_entries(ws: Path) -> list[ConfigEntry]:
    return parse_config_list(await _git(["config", "--local", "--null", "--list"], cwd=ws))


async def _remove_legacy_credentials(ws: Path, remote: str) -> None:
    """Rewrite the workspace-local Git config without credentials; fail if any remain.

    Only ``.git/config`` changes: refs, index, worktree and untracked files are untouched.
    """
    git_path = Path(await _git(["rev-parse", "--git-path", "config"], cwd=ws))
    config_path = git_path if git_path.is_absolute() else ws / git_path
    original = config_path.read_text()
    stripped = strip_credential_sections(original)
    if stripped != original:
        with tempfile.NamedTemporaryFile("w", dir=config_path.parent, delete=False, prefix="config.") as handle:
            handle.write(stripped)
        os.chmod(handle.name, config_path.stat().st_mode & 0o777)
        os.replace(handle.name, config_path)
    commands = credential_cleanup_commands(await _local_config_entries(ws), remote)
    for args in commands:
        await _git(args, cwd=ws)
    remaining = credential_findings(await _local_config_entries(ws), remote)
    if remaining:
        raise DroidRunError(f"workspace Git config still holds {len(remaining)} credential setting(s); not resuming")
    if stripped != original or commands:
        log_event(logger, logging.INFO, "droid.workspace_credentials_removed", workspace=str(ws))


async def _prepare_continuation_workspace(repository: str, ws: Path) -> None:
    """Make an existing task workspace safe to resume in, without setup, clone, fetch or cleanup."""
    if not (ws / ".git").exists():
        raise DroidRunError("task workspace is missing; reassign the issue for a fresh run")
    toplevel = await _git_or_none(["rev-parse", "--show-toplevel"], cwd=ws)
    if toplevel is None or Path(toplevel).resolve() != ws.resolve():
        raise DroidRunError("task workspace is not its own Git repository; not resuming")
    await _remove_legacy_credentials(ws, _remote_url(repository))
    await _require_commit_identity(ws)


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
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        # Open SDK sessions with a turn in flight, kept so a stop can interrupt them.
        self._sessions: dict[str, Session] = {}
        self.stop_grace_seconds = STOP_GRACE_SECONDS

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
            key for key, task in self._tasks.items() if not key.startswith(("launch:", "stop:")) and not task.done()
        }

    # ---- registry -------------------------------------------------------

    def track(self, key: str, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        existing = self._tasks.get(key)
        if existing and not existing.done():
            raise DroidSessionBusyError(f"run {key} is already in flight")
        self._tasks[key] = task
        task.add_done_callback(lambda _t: self._tasks.pop(key, None))
        return task

    def session_busy(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return bool(task and not task.done())

    async def interrupt_run(self, session_id: str | None) -> dict[str, Any]:
        """Ask the SDK to interrupt the turn in flight on ``session_id``; cancel it if it does not wind down.

        Returns what was done. A session with no turn in flight is left alone,
        so a repeated or late stop is harmless.
        """
        result: dict[str, Any] = {"live_turn": False, "interrupt_sent": False, "interrupt_error": None}
        task = self._tasks.get(session_id) if session_id else None
        if session_id is None or task is None or task.done():
            return result
        result["live_turn"] = True
        session = self._sessions.get(session_id)
        interrupt = getattr(session, "interrupt", None)
        if callable(interrupt):
            try:
                await asyncio.wait_for(interrupt(), timeout=INTERRUPT_REQUEST_TIMEOUT_SECONDS)
                result["interrupt_sent"] = True
            except Exception as exc:
                result["interrupt_error"] = safe_error(exc)
        if not self.session_busy(f"stop:{session_id}"):
            self.track(f"stop:{session_id}", asyncio.create_task(self._cancel_if_still_running(task, session_id)))
        log_event(logger, logging.WARNING, "droid.turn_interrupt_requested", session_id=session_id, **result)
        return result

    async def _cancel_if_still_running(self, task: asyncio.Task[Any], session_id: str) -> None:
        done, _pending = await asyncio.wait({task}, timeout=self.stop_grace_seconds)
        if not done:
            task.cancel()
            log_event(logger, logging.WARNING, "droid.turn_cancelled_after_stop", session_id=session_id)

    async def shutdown(self) -> None:
        """Cancel in-flight runs; Droid sessions save state and stay resumable."""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    # ---- settings / discovery -------------------------------------------

    def autonomy(self) -> Autonomy:
        """Autonomy for implementation sessions. Review sessions use ``review_session_config``."""
        level = (get_settings().droid_autonomy or "high").strip().lower()
        return {
            "off": Autonomy.OFF,
            "low": Autonomy.LOW,
            "medium": Autonomy.MEDIUM,
            "high": Autonomy.HIGH,
        }.get(level, Autonomy.HIGH)

    def _fix_session_config(self) -> SessionConfig:
        return SessionConfig(
            autonomy=self.autonomy(),
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

    async def list_available_models(self) -> list[dict[str, Any]]:
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

    async def _run_setup_command(self, ws: Path, command: str) -> str:
        """Run a stored setup command only under explicit privileged-setup opt-in."""
        if not command.strip():
            return SetupStatus.NOT_CONFIGURED
        if not privileged_setup_allowed():
            log_event(
                logger,
                logging.WARNING,
                "droid.setup_command_disabled",
                workspace=str(ws),
                reason="FORGE_ALLOW_PRIVILEGED_SETUP is false",
            )
            return SetupStatus.DISABLED
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=str(ws),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=agent_environment(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=SETUP_TIMEOUT_SECONDS)
        except TimeoutError:
            proc.kill()
            log_event(
                logger,
                logging.WARNING,
                "droid.setup_command_timeout",
                workspace=str(ws),
            )
            return SetupStatus.TIMED_OUT
        if proc.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "droid.setup_command_failed",
                workspace=str(ws),
                exit_code=proc.returncode,
                output=(out or b"").decode(errors="replace")[-500:],
            )
            return SetupStatus.FAILED
        return SetupStatus.SUCCEEDED

    async def prepare_fix_workspace(
        self,
        *,
        repository: str,
        task_id: int,
        starting_ref: str | None,
        setup_command: str = "",
    ) -> Path:
        """Clone/refresh the per-task workspace with a credential-free remote."""
        ws, _setup = await self.prepare_fix_workspace_with_setup(
            repository=repository,
            task_id=task_id,
            starting_ref=starting_ref,
            setup_command=setup_command,
        )
        return ws

    async def prepare_fix_workspace_with_setup(
        self,
        *,
        repository: str,
        task_id: int,
        starting_ref: str | None,
        setup_command: str = "",
    ) -> tuple[Path, str]:
        """Prepare the workspace and report what happened to the setup command."""
        ws = task_workspace_dir(repository, task_id)
        remote = _remote_url(repository)
        ws.parent.mkdir(parents=True, exist_ok=True)

        if (ws / ".git").exists():
            # Reuse (restart recovery for the same task): keep agent state, and
            # scrub legacy credentials (token-bearing origin, auth headers) from its config.
            await _remove_legacy_credentials(ws, remote)
        else:
            ref = (starting_ref or "").strip()
            try:
                if ref:
                    await _git(["clone", "--depth", "1", "--branch", ref, remote, str(ws)], credential_url=remote)
                else:
                    await _git(["clone", "--depth", "1", remote, str(ws)], credential_url=remote)
            except DroidRunError:
                if not ws.exists() or not (ws / ".git").exists():
                    raise
                # Clone partially succeeded (e.g. unknown ref); keep what we got.
        await _require_commit_identity(ws)

        setup = await self._run_setup_command(ws, (setup_command or "").strip())
        return ws, setup

    async def prepare_review_context(
        self,
        *,
        repository: str,
        review_id: int,
        pr_number: int,
        base_ref: str,
    ) -> ReviewContext:
        """Build a clean review checkout of the PR head plus the exact base..head diff.

        ``base_ref`` must be the pull request's actual target branch; it is
        never guessed. A *shallow* clone has no common history with the PR
        branch, so a merge base cannot be found. Use a blobless
        (``--filter=blob:none``) full-history clone; lazily fetched blobs need
        the credential, so checkout and diff run as app-owned authenticated
        Git too. The reviewer never runs Git: it gets a detached, ``git
        clean``-ed checkout of the head SHA and the diff from the merge base to
        that SHA.
        """
        base = review_base_ref(base_ref)
        if base is None:
            raise DroidRunError("review base branch is missing or invalid; it is never guessed")
        ws = review_workspace_dir(repository, review_id)
        remote = _remote_url(repository)
        ws.parent.mkdir(parents=True, exist_ok=True)

        if not (ws / ".git").exists():
            await _git(["clone", "--filter=blob:none", remote, str(ws)], credential_url=remote)
        else:
            await _git(["remote", "set-url", "origin", remote], cwd=ws)
            await _git(["fetch", "--filter=blob:none", "--prune", "origin"], cwd=ws, credential_url=remote)

        branch = f"pr-{pr_number}"
        await _git(
            ["fetch", "--force", "origin", f"pull/{pr_number}/head:{branch}"],
            cwd=ws,
            credential_url=remote,
        )
        head_sha = await _git(["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], cwd=ws)
        await _git(["checkout", "-q", "--force", "--detach", head_sha], cwd=ws, credential_url=remote)
        await _git(["clean", "-q", "-ffdx"], cwd=ws)

        # Always refresh the remote-tracking base: a reused workspace's local
        # branch can be stale and would widen the diff beyond the PR's change.
        base_tracking = f"refs/remotes/origin/{base}"
        base_fetched = True
        try:
            await _git(
                ["fetch", "--force", "origin", f"+refs/heads/{base}:{base_tracking}"],
                cwd=ws,
                credential_url=remote,
            )
        except DroidRunError as exc:
            base_fetched = False
            log_event(
                logger,
                logging.WARNING,
                "droid.review_base_ref_failed",
                review_id=review_id,
                base=base,
                error=str(exc),
            )
        # An unrefreshed tracking ref may be stale, so it never stands in for the current base.
        base_sha = await _git_or_none(["merge-base", base_tracking, head_sha], cwd=ws) if base_fetched else None
        diff = None
        if base_sha and base_sha != head_sha:
            diff = await _git_or_none(
                ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", base_sha, head_sha],
                cwd=ws,
                credential_url=remote,
            )
        return ReviewContext(
            workspace=ws,
            base_ref=base,
            base_sha=base_sha,
            head_sha=head_sha or None,
            diff=diff or "",
            incomplete_reason=assess_diff(base_sha, head_sha, diff),
        )

    # ---- candidate inspection ---------------------------------------------

    async def inspect_workspace(self, repository: str, task_id: int, reported_branch: str | None) -> WorkspaceState:
        """Observe the candidate the agent left: the reported branch tip (else HEAD) and worktree state."""
        ws = task_workspace_dir(repository, task_id)
        head_sha = None
        branch = None
        if reported_branch:
            head_sha = await _git_or_none(["rev-parse", "--verify", f"refs/heads/{reported_branch}^{{commit}}"], cwd=ws)
            branch = reported_branch if head_sha else None
        if not head_sha:
            head_sha = await _git_or_none(["rev-parse", "--verify", "HEAD^{commit}"], cwd=ws)
            current = await _git_or_none(["rev-parse", "--abbrev-ref", "HEAD"], cwd=ws)
            branch = current if current and current != "HEAD" else None
        status = await _git_or_none(["status", "--porcelain"], cwd=ws)
        return WorkspaceState(head_sha=head_sha or None, branch=branch, dirty=status is None or bool(status))

    async def ensure_branch_pushed(self, repository: str, task_id: int, branch: str) -> bool:
        """Push the workspace HEAD to ``branch`` on the canonical remote with the app's credential.

        Pushing is an outward action: it requires GitHub writes, an allowed
        repository and an allowed branch. The push targets the canonical URL,
        not whatever ``origin`` the agent may have reconfigured.
        """
        decision = outward_decision(OutwardAction.BRANCH_PUSH, repository, ref=branch)
        if not decision.allowed:
            log_event(
                logger,
                logging.WARNING,
                "policy.outward_denied",
                action=OutwardAction.BRANCH_PUSH,
                repo=repository,
                ref=branch,
                reason=decision.reason,
            )
            return False
        ws = task_workspace_dir(repository, task_id)
        remote = _remote_url(repository)
        try:
            await _git(["remote", "set-url", "origin", remote], cwd=ws)
        except DroidRunError:
            return False
        try:
            # Always push (never force): agents commit locally only, so new
            # follow-up commits on an existing branch also need publishing.
            await _git(["push", remote, f"HEAD:refs/heads/{branch}"], cwd=ws, credential_url=remote)
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

    @staticmethod
    async def _admit_turn(session: Session, observer: TurnObserver | None, kind: str) -> bool:
        """Last check before the paid turn: an attempt stopped after its session opened never streams."""
        admitted = getattr(observer, "admitted", None)
        if not callable(admitted) or admitted():
            return True
        with contextlib.suppress(Exception):
            await session.close()
        log_event(logger, logging.WARNING, "droid.turn_not_admitted", session_id=session.id, kind=kind)
        return False

    async def _run_turn(
        self,
        session: Session,
        prompt: str,
        *,
        kind: str,
        on_complete: Callable[[dict[str, Any]], Any] | None,
        observer: TurnObserver | None = None,
    ) -> dict[str, Any]:
        session_id = session.id
        outcome: dict[str, Any] = {"session_id": session_id, "kind": kind, "reported_model": _reported_model(session)}
        events = 0
        if not await self._admit_turn(session, observer, kind):
            outcome.update(success=False, subtype="not_admitted", text="", error="attempt is no longer active")
            return outcome
        if observer is not None:
            observer.started(kind)
        try:
            # Partial messages carry the cumulative TokenUsageUpdate snapshots a
            # timed-out or failed turn may never report in a terminal result.
            async with session.stream(prompt, timeout=self.timeout_seconds, include_partial_messages=True) as stream:
                async for message in stream:
                    events += 1
                    if observer is not None:
                        observer.observe(message)
                    if events % 25 == 0:
                        log_event(
                            logger,
                            logging.DEBUG,
                            "droid.turn_progress",
                            session_id=session_id,
                            events=events,
                        )
            outcome.update(_result_outcome(stream.result))
        except asyncio.CancelledError:
            outcome.update(success=False, subtype="cancelled", text="", error="run cancelled")
            raise
        except (RunTimeoutError, TimeoutError):
            outcome.update(
                success=False,
                subtype="timeout",
                text="",
                error=f"turn exceeded its {self.timeout_seconds:g}s timeout",
            )
        except Exception as exc:
            outcome.update(
                success=False,
                subtype="error_during_execution",
                text="",
                error=safe_error(exc),
            )
        finally:
            with contextlib.suppress(Exception):
                await session.close()
            if observer is not None:
                observer.finished(outcome)

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

    async def _open_session(
        self,
        *,
        cwd: Path,
        resume_id: str | None = None,
        model: str | None = None,
        config: SessionConfig | None = None,
        purpose: str = "session",
    ) -> Session:
        """Open a session whose droid process has the clean agent environment."""
        runtime: Runtime | None = None
        try:
            runtime = await open_agent_runtime(cwd=cwd, api_key=self.api_key)
            if resume_id:
                session = Session.resume(resume_id, runtime=runtime, api_key=self.api_key)
            else:
                session = Session(cwd=str(cwd), model=model, config=config, runtime=runtime, api_key=self.api_key)
            await session.open()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await close_runtime(runtime)
            raise DroidRunError(safe_error(f"failed to open Droid {purpose}: {exc}")) from exc
        return session

    def _spawn_turn(
        self,
        session: Session,
        prompt: str,
        *,
        kind: str,
        on_complete: Callable[[dict[str, Any]], Any] | None,
        observer: TurnObserver | None = None,
    ) -> str:
        session_id = session.id
        task = asyncio.create_task(
            self._run_turn(session, prompt, kind=kind, on_complete=on_complete, observer=observer)
        )
        self.track(session_id, task)
        self._sessions[session_id] = session

        def _forget(_task: asyncio.Task[Any]) -> None:
            if self._sessions.get(session_id) is session:
                del self._sessions[session_id]

        task.add_done_callback(_forget)
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
        on_complete: Callable[[dict[str, Any]], Any] | None = None,
        observer: TurnObserver | None = None,
    ) -> dict[str, Any]:
        """Prepare the workspace, open a Droid session and stream the fix turn.

        Returns ``{"session_id": str, "status": "running"}`` once the session
        is open; the turn itself completes in the background.
        """
        if not self.configured:
            raise DroidRunError("Droid is not configured (FACTORY_API_KEY missing)")
        ws, setup = await self.prepare_fix_workspace_with_setup(
            repository=repository,
            task_id=task_id,
            starting_ref=starting_ref,
            setup_command=setup_command,
        )
        # The candidate's base: the commit the agent starts from.
        base_sha = await _git_or_none(["rev-parse", "--verify", "HEAD^{commit}"], cwd=ws)
        prompt = build_fix_prompt(
            repository_url,
            issue_number,
            issue_title,
            issue_body,
            starting_ref or "",
            work_branch=configured_publication_branch(starting_ref),
        )
        session = await self._open_session(
            cwd=ws, model=model or self.model, config=self._fix_session_config(), purpose="session"
        )
        session_id = self._spawn_turn(session, prompt, kind="fix", on_complete=on_complete, observer=observer)
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
            setup=setup,
        )
        return {"session_id": session_id, "status": "running", "setup": setup, "base_sha": base_sha}

    async def start_review_run(
        self,
        *,
        context: ReviewContext,
        repository_url: str,
        review_id: int,
        pr_url: str,
        model: str | None = None,
        on_complete: Callable[[dict[str, Any]], Any] | None = None,
        on_session_opened: Callable[[str], None] | None = None,
        observer: TurnObserver | None = None,
    ) -> dict[str, Any]:
        """Open a *fresh* restricted (Read/Grep/Glob) review session over a complete review context.

        Reviews are never resumed: ``Session.resume`` cannot re-apply tool
        restrictions, so every review turn is a new restricted session.
        ``on_session_opened`` receives the session id before the turn starts,
        so the caller can persist it before any completion callback can run.
        """
        if not self.configured:
            raise DroidRunError("Droid is not configured (FACTORY_API_KEY missing)")
        if not context.complete:
            raise ReviewContextIncompleteError(context)
        prompt = build_review_prompt(context, repository_url=repository_url, pr_url=pr_url)
        session = await self._open_session(
            cwd=context.workspace,
            model=model or self.model,
            config=review_session_config(),
            purpose="review session",
        )
        if on_session_opened is not None:
            try:
                on_session_opened(session.id)
            except Exception:
                with contextlib.suppress(Exception):
                    await session.close()
                raise
        session_id = self._spawn_turn(session, prompt, kind="review", on_complete=on_complete, observer=observer)
        log_event(
            logger,
            logging.INFO,
            "droid.review_started",
            session_id=session_id,
            review_id=review_id,
            base_sha=context.base_sha,
            head_sha=context.head_sha,
            diff_chars=len(context.diff),
            workspace=str(context.workspace),
        )
        return {"session_id": session_id, "status": "running"}

    async def send_follow_up(
        self,
        session_id: str,
        instruction: str,
        *,
        repository: str,
        cwd: Path,
        on_complete: Callable[[dict[str, Any]], Any] | None = None,
        observer: TurnObserver | None = None,
    ) -> dict[str, Any]:
        """Resume a saved session in its task workspace and run a follow-up turn in the background.

        The workspace's legacy credentials are removed (from ``repository``,
        the task's stored repository) before the session opens.
        """
        if not self.configured:
            raise DroidRunError("Droid is not configured (FACTORY_API_KEY missing)")
        if self.session_busy(session_id):
            raise DroidSessionBusyError("session already has a turn in flight")
        prompt = build_follow_up_prompt(instruction)
        await _prepare_continuation_workspace(repository, cwd)
        base_sha = await _git_or_none(["rev-parse", "--verify", "HEAD^{commit}"], cwd=cwd)
        session = await self._open_session(cwd=cwd, resume_id=session_id, purpose=f"session {session_id}")
        self._spawn_turn(session, prompt, kind="follow_up", on_complete=on_complete, observer=observer)
        log_event(logger, logging.INFO, "droid.follow_up_sent", session_id=session_id)
        return {"session_id": session_id, "status": "running", "base_sha": base_sha}

    async def resume_interrupted(
        self,
        session_id: str,
        *,
        repository: str,
        cwd: Path,
        on_complete: Callable[[dict[str, Any]], Any] | None = None,
        observer: TurnObserver | None = None,
    ) -> dict[str, Any]:
        """Resume an interrupted implementation session on explicit operator request.

        Only for fix/follow-up sessions; review sessions are never resumed. As
        with follow-ups, the workspace is cleaned of legacy credentials first.
        Startup never calls this: restart recovery only marks work interrupted.
        """
        if not self.configured:
            raise DroidRunError("Droid is not configured (FACTORY_API_KEY missing)")
        if self.session_busy(session_id):
            raise DroidSessionBusyError("session already has a turn in flight")
        await _prepare_continuation_workspace(repository, cwd)
        base_sha = await _git_or_none(["rev-parse", "--verify", "HEAD^{commit}"], cwd=cwd)
        session = await self._open_session(cwd=cwd, resume_id=session_id, purpose=f"session {session_id}")
        self._spawn_turn(session, RECOVERY_PROMPT_TEMPLATE, kind="recovery", on_complete=on_complete, observer=observer)
        log_event(logger, logging.INFO, "droid.session_recovered", session_id=session_id)
        return {"session_id": session_id, "status": "running", "base_sha": base_sha}


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
    "FIX_PROMPT_TEMPLATE",
    "FOLLOW_UP_PROMPT_TEMPLATE",
    "RECOVERY_PROMPT_TEMPLATE",
    "DroidClient",
    "DroidRunError",
    "DroidSessionBusyError",
    "SetupStatus",
    "build_fix_prompt",
    "build_follow_up_prompt",
    "extract_branch_name",
    "get_droid_client",
    "map_outcome_status",
    "reset_droid_client_for_tests",
    "review_workspace_dir",
    "task_workspace_dir",
    "workspace_root",
]
