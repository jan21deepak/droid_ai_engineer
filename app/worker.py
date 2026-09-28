"""Event-driven Droid run finalization plus GitHub state polling.

Cursor Forge polled a remote API for agent status. Droid Forge sessions run
locally and finish in background asyncio tasks, so completion is event-driven:
``launch_fix_task`` / ``launch_review_task`` start a run and hand the turn
outcome to ``finalize_fix_task`` / ``finalize_review_task``. The worker loop
now only reconciles GitHub-side state (PR open/merged, auto-merge).
"""

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial

import httpx

from app.config import get_settings
from app.database import db_session
from app.droid_client import (
    DroidClient,
    DroidRunError,
    extract_branch_name,
    extract_verdict,
    get_droid_client,
    map_outcome_status,
)
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.repos import resolve_agent_launch_config
from app.timeutil import format_sgt

logger = logging.getLogger("app.worker")

REVIEW_BODY_LIMIT = 60_000


def _format_runtime(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_completion_comment(task: Task) -> str:
    return (
        "✅ **Task completed**\n\n"
        f"- **Droid session:** `{task.droid_session_id}`\n"
        f"- **Pull Request:** {task.pull_request_url or 'n/a'}\n"
        f"- **Runtime:** {_format_runtime(task.duration_seconds)}\n"
        f"- **Completed at:** {format_sgt(task.completed_at) or 'n/a'}\n\n"
        f"**Summary of changes:**\n\n{task.summary or 'No summary available.'}"
    )


def _parse_pr_number(pr_url: str) -> int | None:
    try:
        parts = pr_url.rstrip("/").split("/")
        if "pull" in parts:
            return int(parts[parts.index("pull") + 1])
    except (ValueError, IndexError):
        return None
    return None


def _persist_task_pr_url(task_id: int, pr_url: str) -> None:
    """Write PR URL immediately so a later crash cannot lose the link."""
    if not pr_url:
        return
    with db_session() as session:
        db_task = session.get(Task, task_id)
        if db_task and not db_task.pull_request_url:
            db_task.pull_request_url = pr_url
            if not db_task.pr_state:
                db_task.pr_state = "open"


# ---------------------------------------------------------------------------
# Fix runs
# ---------------------------------------------------------------------------

async def launch_fix_task(
    task_id: int,
    *,
    repository: str,
    repository_url: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
) -> None:
    """Background launcher: prepare the workspace and start the Droid fix session."""
    droid = get_droid_client()
    launch = resolve_agent_launch_config(
        repository=repository, repository_url=repository_url
    )
    try:
        result = await droid.start_fix_run(
            repository=repository,
            repository_url=repository_url,
            task_id=task_id,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body or "",
            starting_ref=launch.get("starting_ref"),
            setup_command=launch.get("setup_command") or "",
            model=launch.get("model"),
            on_complete=partial(finalize_fix_task, task_id),
        )
    except Exception as exc:
        with db_session() as session:
            db_task = session.get(Task, task_id)
            if db_task is not None:
                db_task.status = TaskStatus.FAILED
                db_task.error = f"Droid session failed to start: {exc}"
        log_event(
            logger,
            logging.ERROR,
            "droid.session_start_failed",
            task_id=task_id,
            repository=repository,
            issue=issue_number,
            error=str(exc),
        )
        return

    with db_session() as session:
        db_task = session.get(Task, task_id)
        if db_task is not None:
            db_task.droid_session_id = result["session_id"]
            db_task.status = TaskStatus.RUNNING
            db_task.error = None
    log_event(
        logger,
        logging.INFO,
        "droid.agent_linked",
        task_id=task_id,
        session_id=result["session_id"],
        repository=repository,
        issue=issue_number,
        model=launch.get("model") or get_settings().droid_model,
    )


def spawn_fix_launch(task_id: int, **launch_kwargs) -> asyncio.Task:
    """Fire the launcher in the background so webhook handlers stay fast."""
    droid = get_droid_client()
    task = asyncio.create_task(launch_fix_task(task_id, **launch_kwargs))
    droid.track(f"launch:task-{task_id}", task)
    return task


async def ensure_pull_request_for_task(
    task: Task,
    branch: str,
    github: GitHubClient,
) -> str | None:
    """Open a same-repo PR with the forge GitHub token after the agent pushes a branch.

    Fix sessions are told not to open pull requests themselves; forge opens the
    PR *within* ``task.repository`` (``head=owner:branch``, base = fork default /
    starting_ref) so review + merge tracking can continue.
    """
    if task.pull_request_url:
        return task.pull_request_url
    if not branch:
        return None

    repository = task.repository
    existing = await github.find_open_pull_request_for_head(repository, branch)
    if existing and existing.get("html_url"):
        log_event(
            logger,
            logging.INFO,
            "github.pr_reused",
            task_id=task.id,
            repo=repository,
            branch=branch,
            url=existing.get("html_url"),
        )
        _persist_task_pr_url(task.id, existing["html_url"])
        return existing["html_url"]

    launch = resolve_agent_launch_config(
        repository=repository, repository_url=task.repository_url
    )
    base = launch.get("starting_ref")
    if not base:
        try:
            meta = await github.get_repository(repository)
            base = meta.get("default_branch") or "main"
        except Exception:
            base = "main"

    title = task.issue_title or f"Fix #{task.issue_number}"
    if task.issue_number and f"#{task.issue_number}" not in title:
        title = f"{title} (#{task.issue_number})"
    body_parts = [
        f"Opened automatically by Droid Forge as a **same-repo** PR on `{repository}`",
        f"after the Droid agent pushed `{branch}`.",
        "",
        f"Fixes #{task.issue_number}." if task.issue_number else "",
        "",
        f"Droid session: `{task.droid_session_id}`",
    ]
    body = "\n".join(part for part in body_parts if part is not None).strip()

    try:
        pr = await github.create_pull_request(
            repository,
            title=title[:250],
            head=branch,
            base=base,
            body=body,
            same_repo=True,
        )
    except httpx.HTTPStatusError as exc:
        # Race: PR may have been created between find and create.
        if exc.response.status_code == 422:
            existing = await github.find_open_pull_request_for_head(repository, branch)
            if existing and existing.get("html_url"):
                _persist_task_pr_url(task.id, existing["html_url"])
                return existing["html_url"]
        log_event(
            logger,
            logging.WARNING,
            "github.pr_fallback_failed",
            task_id=task.id,
            repo=repository,
            branch=branch,
            base=base,
            error=str(exc),
        )
        return None
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "github.pr_fallback_failed",
            task_id=task.id,
            repo=repository,
            branch=branch,
            base=base,
            error=str(exc),
        )
        return None

    pr_url = pr.get("html_url")
    # Persist before anything else so a crash still leaves the PR link on the task.
    if pr_url:
        _persist_task_pr_url(task.id, pr_url)
    return pr_url


async def finalize_fix_task(task_id: int, outcome: dict) -> None:
    """Turn-completion handler for issue-fix / follow-up / recovery runs."""
    github = GitHubClient()
    droid = get_droid_client()
    settings = get_settings()
    status = map_outcome_status(outcome)
    summary = (outcome.get("text") or "").strip() or None

    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            log_event(logger, logging.WARNING, "droid.task_missing", task_id=task_id)
            return
        old_status = task.status
        task.status = status
        if summary:
            task.summary = summary
        if outcome.get("duration_seconds") is not None:
            task.duration_seconds = outcome["duration_seconds"]
        if outcome.get("credits") is not None:
            task.factory_credits = outcome["credits"]
        if outcome.get("tokens") is not None:
            task.estimated_tokens = outcome["tokens"]
        cost = settings.droid_usd_per_agent_run or None
        if status == TaskStatus.COMPLETED and cost is not None:
            task.cost_usd = cost
        if status == TaskStatus.FAILED:
            task.error = outcome.get("error") or (
                f"Droid run ended with status: {outcome.get('subtype')}"
            )
        if task.completed_at is None:
            task.completed_at = datetime.now(timezone.utc)
        refreshed = task

    log_event(
        logger,
        logging.INFO,
        "task.status_changed",
        task_id=task_id,
        session_id=refreshed.droid_session_id,
        old=old_status,
        new=status,
    )

    if status != TaskStatus.COMPLETED:
        log_event(
            logger,
            logging.WARNING,
            "task.failed",
            task_id=task_id,
            session_id=refreshed.droid_session_id,
            error=refreshed.error,
        )
        return

    log_event(
        logger,
        logging.INFO,
        "task.completed",
        task_id=task_id,
        session_id=refreshed.droid_session_id,
        pr=refreshed.pull_request_url,
        runtime_seconds=refreshed.duration_seconds,
    )

    pr_url = refreshed.pull_request_url
    if not pr_url:
        branch = extract_branch_name(summary) or await droid.current_branch(
            refreshed.repository, task_id
        )
        if branch:
            try:
                await droid.ensure_branch_pushed(refreshed.repository, task_id, branch)
            except DroidRunError as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "droid.branch_push_error",
                    task_id=task_id,
                    branch=branch,
                    error=str(exc),
                )
            try:
                pr_url = await ensure_pull_request_for_task(refreshed, branch, github)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "polling.pr_fallback_error",
                    task_id=task_id,
                    error=str(exc),
                )

    await github.post_issue_comment(
        refreshed.repository,
        refreshed.issue_number,
        build_completion_comment(refreshed),
    )

    if pr_url:
        pr_number = _parse_pr_number(pr_url)
        if pr_number:
            await start_pr_review(
                repository=refreshed.repository,
                repository_url=refreshed.repository_url,
                pr_number=pr_number,
                pr_url=pr_url,
                pr_title=refreshed.issue_title,
            )


# ---------------------------------------------------------------------------
# Review runs
# ---------------------------------------------------------------------------

def ensure_review_task_row(
    *,
    repository: str,
    repository_url: str = "",
    pr_number: int,
    pr_url: str = "",
    pr_title: str = "",
) -> tuple[int | None, bool]:
    """Create a ReviewTask row unless an active one already exists.

    Returns ``(review_id, created)``.
    """
    if not repository or not pr_number:
        return None, False
    with db_session() as session:
        active = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.repository == repository,
                ReviewTask.pr_number == pr_number,
                ReviewTask.status.in_(TaskStatus.ACTIVE),
            )
            .first()
        )
        if active:
            return active.id, False

        review = ReviewTask(
            repository=repository,
            repository_url=repository_url or f"https://github.com/{repository}",
            pr_number=pr_number,
            pr_title=pr_title or f"PR #{pr_number}",
            pr_url=pr_url or f"https://github.com/{repository}/pull/{pr_number}",
            status=TaskStatus.QUEUED,
        )
        session.add(review)
        session.flush()
        log_event(
            logger,
            logging.INFO,
            "droid.review_tracked",
            review_id=review.id,
            repo=repository,
            pr=pr_number,
        )
        return review.id, True


async def start_pr_review(
    *,
    repository: str,
    repository_url: str = "",
    pr_number: int,
    pr_url: str = "",
    pr_title: str = "",
) -> dict:
    """Ensure a review row exists and launch a Droid review session (non-blocking)."""
    review_id, created = ensure_review_task_row(
        repository=repository,
        repository_url=repository_url,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_title=pr_title,
    )
    if review_id is None:
        return {"review_id": None, "started": False, "detail": "invalid review target"}

    droid = get_droid_client()
    if not droid.configured:
        return {"review_id": review_id, "started": False, "detail": "droid_not_configured"}

    launch = asyncio.create_task(
        launch_review_task(
            review_id,
            repository=repository,
            repository_url=repository_url,
            pr_number=pr_number,
            pr_url=pr_url,
        )
    )
    try:
        droid.track(f"launch:review-{review_id}", launch)
    except DroidRunError:
        launch.cancel()
        return {"review_id": review_id, "started": False, "detail": "already_running"}
    return {"review_id": review_id, "started": True, "detail": "droid_review_started"}


async def launch_review_task(
    review_id: int,
    *,
    repository: str,
    repository_url: str,
    pr_number: int,
    pr_url: str,
) -> None:
    """Background launcher for the Droid PR review session."""
    droid = get_droid_client()
    launch = resolve_agent_launch_config(
        repository=repository, repository_url=repository_url
    )
    base_ref = launch.get("starting_ref")
    if not base_ref:
        try:
            meta = await GitHubClient().get_repository(repository)
            base_ref = meta.get("default_branch") or "main"
        except Exception:
            base_ref = "main"

    try:
        result = await droid.start_review_run(
            repository=repository,
            repository_url=repository_url or f"https://github.com/{repository}",
            review_id=review_id,
            pr_number=pr_number,
            pr_url=pr_url or f"https://github.com/{repository}/pull/{pr_number}",
            base_ref=base_ref,
            model=launch.get("model"),
            on_complete=partial(finalize_review_task, review_id),
        )
    except Exception as exc:
        with db_session() as session:
            review = session.get(ReviewTask, review_id)
            if review is not None:
                review.status = TaskStatus.FAILED
                review.error = f"Droid review session failed to start: {exc}"
                review.completed_at = datetime.now(timezone.utc)
        log_event(
            logger,
            logging.ERROR,
            "droid.review_start_failed",
            review_id=review_id,
            repo=repository,
            pr=pr_number,
            error=str(exc),
        )
        return

    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is not None:
            review.droid_session_id = result["session_id"]
            review.status = TaskStatus.RUNNING
            review.error = None
    log_event(
        logger,
        logging.INFO,
        "droid.review_running",
        review_id=review_id,
        session_id=result["session_id"],
        repo=repository,
        pr=pr_number,
    )


async def finalize_review_task(review_id: int, outcome: dict) -> None:
    """Turn-completion handler for Droid PR review sessions."""
    github = GitHubClient()
    settings = get_settings()
    status = map_outcome_status(outcome)
    summary = (outcome.get("text") or "").strip() or None

    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is None:
            log_event(logger, logging.WARNING, "droid.review_missing", review_id=review_id)
            return
        old_status = review.status
        review.status = status
        if summary:
            review.summary = summary
        if outcome.get("duration_seconds") is not None:
            review.duration_seconds = outcome["duration_seconds"]
        if outcome.get("credits") is not None:
            review.factory_credits = outcome["credits"]
        cost = settings.droid_usd_per_agent_run or None
        if status == TaskStatus.COMPLETED and cost is not None:
            review.cost_usd = cost
        if status == TaskStatus.FAILED:
            review.error = outcome.get("error") or (
                f"Droid review ended with status: {outcome.get('subtype')}"
            )
        if review.completed_at is None:
            review.completed_at = datetime.now(timezone.utc)
        refreshed = review

    log_event(
        logger,
        logging.INFO,
        "review.status_changed",
        review_id=review_id,
        old=old_status,
        new=status,
    )

    if status != TaskStatus.COMPLETED or not summary:
        log_event(
            logger,
            logging.WARNING,
            "review.failed",
            review_id=review_id,
            error=refreshed.error,
        )
        return

    verdict = extract_verdict(summary)
    event = {"APPROVE": "APPROVE", "REQUEST_CHANGES": "REQUEST_CHANGES"}.get(
        verdict or "", "COMMENT"
    )
    body = summary[:REVIEW_BODY_LIMIT]
    posted = False
    try:
        await github.create_pull_request_review(
            refreshed.repository, refreshed.pr_number, body, event=event
        )
        posted = True
    except httpx.HTTPStatusError as exc:
        # GitHub rejects APPROVE / REQUEST_CHANGES on the token owner's own PRs.
        if exc.response.status_code in (403, 422):
            try:
                await github.create_pull_request_review(
                    refreshed.repository, refreshed.pr_number, body, event="COMMENT"
                )
                posted = True
                event = "COMMENT"
            except Exception as fallback_exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "github.review_post_failed",
                    review_id=review_id,
                    error=str(fallback_exc),
                )
        else:
            log_event(
                logger,
                logging.WARNING,
                "github.review_post_failed",
                review_id=review_id,
                error=str(exc),
            )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "github.review_post_failed",
            review_id=review_id,
            error=str(exc),
        )

    log_event(
        logger,
        logging.INFO,
        "review.completed",
        review_id=review_id,
        repo=refreshed.repository,
        pr=refreshed.pr_number,
        verdict=event,
        posted=posted,
    )

    if settings.review_auto_merge and event == "APPROVE":
        merged, auto_merge, error = await _attempt_auto_merge(github, refreshed)
        if error and not merged and not auto_merge:
            log_event(
                logger,
                logging.WARNING,
                "review.merge_deferred",
                review_id=review_id,
                error=error,
            )
        if merged or auto_merge:
            with db_session() as session:
                db_review = session.get(ReviewTask, review_id)
                if db_review is not None:
                    db_review.merged = merged or db_review.merged
                    db_review.auto_merge_enabled = auto_merge


# ---------------------------------------------------------------------------
# GitHub state reconciliation (worker loop)
# ---------------------------------------------------------------------------

async def refresh_pr_states(github: GitHubClient, tasks: list[Task]) -> int:
    """Sync each Droid-opened PR's open/closed/merged state from GitHub."""
    updated = 0
    for task in tasks:
        if task.pr_state == "merged":
            continue
        pr_number = _parse_pr_number(task.pull_request_url or "")
        if not pr_number:
            continue
        try:
            pr = await github.get_pull_request(task.repository, pr_number)
        except httpx.HTTPError as exc:
            log_event(
                logger,
                logging.WARNING,
                "pr.state_refresh_failed",
                task_id=task.id,
                pr=task.pull_request_url,
                error=str(exc),
            )
            continue

        merged_at = _parse_iso(pr.get("merged_at"))
        if pr.get("merged") or merged_at:
            state = "merged"
        elif pr.get("state") == "closed":
            state = "closed"
        else:
            state = "open"
        if state == task.pr_state:
            continue

        with db_session() as session:
            db_task = session.get(Task, task.id)
            db_task.pr_state = state
            db_task.pr_merged_at = merged_at
            if state == "merged" and db_task.status in TaskStatus.ACTIVE:
                db_task.status = TaskStatus.COMPLETED
                if not db_task.completed_at:
                    db_task.completed_at = datetime.now(timezone.utc)
        updated += 1
        log_event(
            logger,
            logging.INFO,
            "pr.state_changed",
            task_id=task.id,
            pr=task.pull_request_url,
            state=state,
        )
    return updated


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def finalize_merged_running_tasks() -> int:
    """Complete any still-active fix tasks whose PR is already known merged."""
    updated = 0
    with db_session() as session:
        stuck = (
            session.query(Task)
            .filter(
                Task.status.in_(TaskStatus.ACTIVE),
                Task.pr_state == "merged",
            )
            .all()
        )
        for task in stuck:
            task.status = TaskStatus.COMPLETED
            if not task.completed_at:
                task.completed_at = datetime.now(timezone.utc)
            updated += 1
            log_event(
                logger,
                logging.INFO,
                "task.completed_via_merged_pr",
                task_id=task.id,
                pr=task.pull_request_url,
            )
    return updated


async def _attempt_auto_merge(github: GitHubClient, review: ReviewTask) -> tuple[bool, bool, str | None]:
    """Try to merge immediately; fall back to enabling GitHub auto-merge.

    No-op when ``Settings.review_auto_merge`` is False (default) so humans can
    review before merge.
    """
    if not get_settings().review_auto_merge:
        return False, False, None
    try:
        await github.merge_pull_request(
            review.repository,
            review.pr_number,
            merge_method="squash",
            commit_title=f"{review.pr_title} (#{review.pr_number})",
        )
        return True, False, None
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("message") or ""
        except Exception:
            detail = str(exc)
        if exc.response.status_code in (405, 409, 422):
            enabled = await github.enable_auto_merge(review.repository, review.pr_number)
            if enabled:
                return False, True, None
            return False, False, detail or "merge blocked and auto-merge unavailable"
        return False, False, detail or str(exc)
    except (httpx.HTTPError, RuntimeError) as exc:
        return False, False, str(exc)


async def poll_github_state(github: GitHubClient | None = None) -> int:
    """One reconciliation pass over GitHub-side PR / merge state."""
    github = github or GitHubClient()
    updated = 0

    with db_session() as session:
        tasks_with_pr = (
            session.query(Task)
            .filter(Task.pull_request_url.isnot(None))
            .all()
        )
    updated += await refresh_pr_states(github, tasks_with_pr)
    updated += await finalize_merged_running_tasks()

    settings = get_settings()
    with db_session() as session:
        pending = (
            session.query(ReviewTask)
            .filter(ReviewTask.status == TaskStatus.COMPLETED, ReviewTask.merged.is_(False))
            .all()
        )

    for review in pending:
        try:
            pr = await github.get_pull_request(review.repository, review.pr_number)
        except httpx.HTTPError as exc:
            log_event(
                logger,
                logging.WARNING,
                "review.merge_poll_error",
                review_id=review.id,
                error=str(exc),
            )
            continue
        if pr.get("merged") or (pr.get("state") == "closed" and pr.get("merged_at")):
            with db_session() as session:
                db_review = session.get(ReviewTask, review.id)
                db_review.merged = True
                if not db_review.completed_at:
                    db_review.completed_at = datetime.now(timezone.utc)
            updated += 1
            log_event(
                logger,
                logging.INFO,
                "review.auto_merged",
                review_id=review.id,
                pr=review.pr_url,
            )
        elif pr.get("state") == "closed" and not pr.get("merged"):
            continue
        elif settings.review_auto_merge:
            merged, auto_merge, _ = await _attempt_auto_merge(github, review)
            if merged or auto_merge:
                with db_session() as session:
                    db_review = session.get(ReviewTask, review.id)
                    if merged:
                        db_review.merged = True
                        if not db_review.completed_at:
                            db_review.completed_at = datetime.now(timezone.utc)
                    if auto_merge:
                        db_review.auto_merge_enabled = True
                updated += 1

    return updated


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

async def recover_interrupted_tasks(droid: DroidClient | None = None) -> int:
    """Resume sessions orphaned by a service restart; fail rows that cannot run.

    Droid sessions persist on disk, so a RUNNING row whose session is no longer
    streaming can be resumed with a continuation prompt. Rows that never got a
    session (dispatch interrupted) are marked failed.
    """
    droid = droid or get_droid_client()
    recovered = 0
    if not droid.configured:
        return recovered

    with db_session() as session:
        fix_rows = (
            session.query(Task)
            .filter(Task.status.in_(TaskStatus.ACTIVE))
            .all()
        )
        fix_snapshots = [
            (t.id, t.droid_session_id) for t in fix_rows
        ]
        review_rows = (
            session.query(ReviewTask)
            .filter(ReviewTask.status.in_(TaskStatus.ACTIVE))
            .all()
        )
        review_snapshots = [(r.id, r.droid_session_id) for r in review_rows]

    for task_id, session_id in fix_snapshots:
        if session_id and session_id in droid.active_sessions:
            continue
        if not session_id:
            with db_session() as session:
                db_task = session.get(Task, task_id)
                if db_task is not None:
                    db_task.status = TaskStatus.FAILED
                    db_task.error = "dispatch interrupted by service restart"
            log_event(
                logger, logging.WARNING, "recovery.dispatch_lost", task_id=task_id
            )
            continue
        try:
            await droid.resume_interrupted(
                session_id, on_complete=partial(finalize_fix_task, task_id)
            )
            with db_session() as session:
                db_task = session.get(Task, task_id)
                if db_task is not None:
                    db_task.status = TaskStatus.RUNNING
            recovered += 1
        except Exception as exc:
            with db_session() as session:
                db_task = session.get(Task, task_id)
                if db_task is not None:
                    db_task.status = TaskStatus.FAILED
                    db_task.error = f"session lost after service restart: {exc}"
            log_event(
                logger,
                logging.WARNING,
                "recovery.session_lost",
                task_id=task_id,
                session_id=session_id,
                error=str(exc),
            )

    for review_id, session_id in review_snapshots:
        if session_id and session_id in droid.active_sessions:
            continue
        if not session_id:
            with db_session() as session:
                db_review = session.get(ReviewTask, review_id)
                if db_review is not None:
                    db_review.status = TaskStatus.FAILED
                    db_review.error = "dispatch interrupted by service restart"
                    db_review.completed_at = datetime.now(timezone.utc)
            log_event(
                logger, logging.WARNING, "recovery.dispatch_lost", review_id=review_id
            )
            continue
        try:
            await droid.resume_interrupted(
                session_id, on_complete=partial(finalize_review_task, review_id)
            )
            with db_session() as session:
                db_review = session.get(ReviewTask, review_id)
                if db_review is not None:
                    db_review.status = TaskStatus.RUNNING
            recovered += 1
        except Exception as exc:
            with db_session() as session:
                db_review = session.get(ReviewTask, review_id)
                if db_review is not None:
                    db_review.status = TaskStatus.FAILED
                    db_review.error = f"session lost after service restart: {exc}"
                    db_review.completed_at = datetime.now(timezone.utc)
            log_event(
                logger,
                logging.WARNING,
                "recovery.session_lost",
                review_id=review_id,
                session_id=session_id,
                error=str(exc),
            )

    if recovered or fix_snapshots or review_snapshots:
        log_event(
            logger,
            logging.INFO,
            "recovery.completed",
            recovered=recovered,
            fixes=len(fix_snapshots),
            reviews=len(review_snapshots),
        )
    return recovered


async def worker_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = settings.poll_interval_seconds
    log_event(logger, logging.INFO, "worker.started", interval_seconds=interval)
    while not stop_event.is_set():
        try:
            await poll_github_state()
        except Exception as exc:
            log_event(logger, logging.ERROR, "worker.iteration_error", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log_event(logger, logging.INFO, "worker.stopped")
