"""Event-driven Droid run finalization plus GitHub state polling.

Cursor Forge polled a remote API for agent status. Droid Forge sessions run
locally and finish in background asyncio tasks, so completion is event-driven:
``launch_fix_task`` / ``launch_review_task`` start a run and hand the turn
outcome to ``finalize_fix_task`` / ``finalize_review_task``. The worker loop
(``app.reconcile``) only reconciles GitHub-side state.

Every launch, finalization, review and recovery step re-checks repository and
outward-action policy (``app.policy``) before any Git or GitHub call, so stored
work cannot bypass the current configuration.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

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
    review_workspace_dir,
    task_workspace_dir,
)
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.policy import OutwardAction, PolicyDecision, outward_decision, repository_decision
from app.reconcile import (
    attempt_auto_merge,
    finalize_merged_running_tasks,
    parse_pr_number,
    poll_github_state,
    refresh_pr_states,
    worker_loop,
)
from app.redaction import safe_error
from app.repos import resolve_agent_launch_config
from app.timeutil import format_sgt

__all__ = [
    "finalize_merged_running_tasks",
    "parse_pr_number",
    "poll_github_state",
    "refresh_pr_states",
    "worker_loop",
]

logger = logging.getLogger("app.worker")

REVIEW_BODY_LIMIT = 60_000
# Pseudo-action for read/work access to a repository (no GitHub write).
REPOSITORY_ACCESS = "repository_access"


def _policy_denied(action: str, repository: str, *, ref: str | None = None, **ctx: Any) -> PolicyDecision | None:
    """Check an outward action (or plain repository access) and log a denial."""
    if action == REPOSITORY_ACCESS:
        decision = repository_decision(repository)
    else:
        decision = outward_decision(action, repository, ref=ref)
    if decision.allowed:
        return None
    log_event(
        logger,
        logging.WARNING,
        "policy.outward_denied",
        action=action,
        repo=repository,
        ref=ref,
        reason=decision.reason,
        **ctx,
    )
    return decision


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
    denied = _policy_denied(REPOSITORY_ACCESS, repository, task_id=task_id)
    if denied is not None:
        with db_session() as session:
            db_task = session.get(Task, task_id)
            if db_task is not None:
                db_task.status = TaskStatus.FAILED
                db_task.error = f"blocked by repository policy: {denied.reason}"
        return
    droid = get_droid_client()
    launch = resolve_agent_launch_config(repository=repository, repository_url=repository_url)
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
                db_task.error = f"Droid session failed to start: {safe_error(exc)}"
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
        setup=result.get("setup"),
    )


def spawn_fix_launch(task_id: int, **launch_kwargs: Any) -> asyncio.Task[None]:
    """Fire the launcher in the background so webhook handlers stay fast."""
    droid = get_droid_client()
    task = asyncio.create_task(launch_fix_task(task_id, **launch_kwargs))
    droid.track(f"launch:task-{task_id}", task)
    return task


async def _pull_request_base(task: Task, github: GitHubClient) -> str:
    launch = resolve_agent_launch_config(repository=task.repository, repository_url=task.repository_url)
    base = launch.get("starting_ref")
    if base:
        return base
    try:
        meta = await github.get_repository(task.repository)
        return str(meta.get("default_branch") or "main")
    except Exception:
        return "main"


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
    if _policy_denied(OutwardAction.PULL_REQUEST_CREATE, task.repository, ref=branch, task_id=task.id):
        return None

    repository = task.repository
    existing = await github.find_open_pull_request_for_head(repository, branch)
    if existing and existing.get("html_url"):
        html_url: str = existing["html_url"]
        log_event(
            logger,
            logging.INFO,
            "github.pr_reused",
            task_id=task.id,
            repo=repository,
            branch=branch,
            url=html_url,
        )
        _persist_task_pr_url(task.id, html_url)
        return html_url

    base = await _pull_request_base(task, github)
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
                reused_url: str = existing["html_url"]
                _persist_task_pr_url(task.id, reused_url)
                return reused_url
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

    pr_url: str | None = pr.get("html_url")
    # Persist before anything else so a crash still leaves the PR link on the task.
    if pr_url:
        _persist_task_pr_url(task.id, pr_url)
    return pr_url


def _apply_outcome_to_task(task: Task | ReviewTask, outcome: dict[str, Any], status: str, summary: str | None) -> None:
    """Persist turn outcome fields (status, summary, runtime, tokens, cost) onto a task row."""
    settings = get_settings()
    task.status = status
    if summary:
        task.summary = summary
    if outcome.get("duration_seconds") is not None:
        task.duration_seconds = outcome["duration_seconds"]
    if outcome.get("credits") is not None:
        task.factory_credits = outcome["credits"]
    if outcome.get("tokens") is not None and isinstance(task, Task):
        # Only fix tasks track estimated_tokens; ReviewTask has no such column.
        task.estimated_tokens = outcome["tokens"]
    cost = settings.droid_usd_per_agent_run or None
    if status == TaskStatus.COMPLETED and cost is not None:
        task.cost_usd = cost
    if status == TaskStatus.FAILED:
        task.error = outcome.get("error") or (f"Droid run ended with status: {outcome.get('subtype')}")
    if task.completed_at is None:
        task.completed_at = datetime.now(UTC)


async def _publish_task_branch(task: Task, summary: str | None, droid: DroidClient) -> str | None:
    """Push the agent's branch with the app's credential; agents cannot push themselves.

    Returns the branch once it is published, else ``None`` (policy denied or push failed).
    """
    branch = extract_branch_name(summary) or await droid.current_branch(task.repository, task.id)
    if not branch:
        return None
    if _policy_denied(OutwardAction.BRANCH_PUSH, task.repository, ref=branch, task_id=task.id):
        return None
    try:
        pushed = await droid.ensure_branch_pushed(task.repository, task.id, branch)
    except DroidRunError as exc:
        log_event(
            logger,
            logging.WARNING,
            "droid.branch_push_error",
            task_id=task.id,
            branch=branch,
            error=str(exc),
        )
        return None
    return branch if pushed else None


async def _resolve_pr_url(task: Task, branch: str | None, github: GitHubClient) -> str | None:
    """Best-effort fallback: open the PR for a published branch."""
    if not branch:
        return None
    try:
        return await ensure_pull_request_for_task(task, branch, github)
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "polling.pr_fallback_error",
            task_id=task.id,
            error=str(exc),
        )
        return None


async def finalize_fix_task(task_id: int, outcome: dict[str, Any]) -> None:
    """Turn-completion handler for issue-fix / follow-up / recovery runs."""
    github = GitHubClient()
    droid = get_droid_client()
    status = map_outcome_status(outcome)
    summary = (outcome.get("text") or "").strip() or None

    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            log_event(logger, logging.WARNING, "droid.task_missing", task_id=task_id)
            return
        old_status = task.status
        _apply_outcome_to_task(task, outcome, status, summary)
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

    if _policy_denied(REPOSITORY_ACCESS, refreshed.repository, task_id=task_id):
        return
    branch = await _publish_task_branch(refreshed, summary, droid)
    pr_url = refreshed.pull_request_url or await _resolve_pr_url(refreshed, branch, github)
    # Keep the completion comment accurate: the PR may have just been created
    # above, and `refreshed` is a detached snapshot that does not see it.
    if pr_url and not refreshed.pull_request_url:
        refreshed.pull_request_url = pr_url

    if not _policy_denied(OutwardAction.ISSUE_COMMENT, refreshed.repository, task_id=task_id):
        await github.post_issue_comment(
            refreshed.repository,
            refreshed.issue_number,
            build_completion_comment(refreshed),
        )

    if pr_url:
        pr_number = parse_pr_number(pr_url)
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
) -> dict[str, Any]:
    """Ensure a review row exists and launch a Droid review session (non-blocking)."""
    if _policy_denied(REPOSITORY_ACCESS, repository, pr=pr_number):
        return {"review_id": None, "started": False, "detail": "repository_not_allowed"}
    review_id, _created = ensure_review_task_row(
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
    denied = _policy_denied(REPOSITORY_ACCESS, repository, review_id=review_id)
    if denied is not None:
        _mark_row_failed(ReviewTask, review_id, f"blocked by repository policy: {denied.reason}", stamp_completed=True)
        return
    droid = get_droid_client()
    launch = resolve_agent_launch_config(repository=repository, repository_url=repository_url)
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
                review.error = f"Droid review session failed to start: {safe_error(exc)}"
                review.completed_at = datetime.now(UTC)
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


async def _post_review_verdict(github: GitHubClient, review: ReviewTask, body: str, event: str) -> tuple[bool, str]:
    """Post a PR review, falling back to COMMENT when GitHub rejects the verdict.

    Returns ``(posted, event)``.
    """
    if _policy_denied(OutwardAction.PULL_REQUEST_REVIEW, review.repository, review_id=review.id):
        return False, event
    try:
        await github.create_pull_request_review(review.repository, review.pr_number, body, event=event)
        return True, event
    except httpx.HTTPStatusError as exc:
        # GitHub rejects APPROVE / REQUEST_CHANGES on the token owner's own PRs.
        if exc.response.status_code in (403, 422):
            try:
                await github.create_pull_request_review(review.repository, review.pr_number, body, event="COMMENT")
                return True, "COMMENT"
            except Exception as fallback_exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "github.review_post_failed",
                    review_id=review.id,
                    error=str(fallback_exc),
                )
        else:
            log_event(
                logger,
                logging.WARNING,
                "github.review_post_failed",
                review_id=review.id,
                error=str(exc),
            )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "github.review_post_failed",
            review_id=review.id,
            error=str(exc),
        )
    return False, event


async def finalize_review_task(review_id: int, outcome: dict[str, Any]) -> None:
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
        _apply_outcome_to_task(review, outcome, status, summary)
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
    event = {"APPROVE": "APPROVE", "REQUEST_CHANGES": "REQUEST_CHANGES"}.get(verdict or "", "COMMENT")
    body = summary[:REVIEW_BODY_LIMIT]
    posted, event = await _post_review_verdict(github, refreshed, body, event)

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
        merged, auto_merge, error = await attempt_auto_merge(github, refreshed)
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
# Restart recovery
# ---------------------------------------------------------------------------


def _mark_row_failed[TaskOrReviewRow: Task | ReviewTask](
    model: type[TaskOrReviewRow], row_id: int, error: str, *, stamp_completed: bool = False
) -> None:
    """Mark a Task/ReviewTask row failed with the given error message."""
    with db_session() as session:
        row = session.get(model, row_id)
        if row is not None:
            row.status = TaskStatus.FAILED
            row.error = error
            if stamp_completed:
                row.completed_at = datetime.now(UTC)


def _fail_rows_outside_policy(
    model: type[Task] | type[ReviewTask],
    snapshots: list[tuple[int, str | None, str]],
    *,
    stamp_completed: bool,
    id_key: str,
) -> None:
    for row_id, _session_id, repository in snapshots:
        denied = _policy_denied(REPOSITORY_ACCESS, repository, row=f"{id_key}={row_id}")
        if denied is not None:
            _mark_row_failed(
                model, row_id, f"blocked by repository policy: {denied.reason}", stamp_completed=stamp_completed
            )


async def _resume_orphaned_row[TaskOrReviewRow: Task | ReviewTask](
    model: type[TaskOrReviewRow],
    row_id: int,
    session_id: str | None,
    repository: str,
    droid: DroidClient,
    finalize: Callable[[int, dict[str, Any]], Awaitable[None]],
    *,
    stamp_completed: bool,
    id_key: str,
) -> int:
    """Resume one orphaned row's Droid session; fail the row if the session is lost.

    Returns 1 when the row was resumed, 0 when it was marked failed.
    """
    denied = _policy_denied(REPOSITORY_ACCESS, repository, row=f"{id_key}={row_id}")
    if denied is not None:
        _mark_row_failed(
            model, row_id, f"blocked by repository policy: {denied.reason}", stamp_completed=stamp_completed
        )
        return 0
    if not session_id:
        _mark_row_failed(model, row_id, "dispatch interrupted by service restart", stamp_completed=stamp_completed)
        log_event(logger, logging.WARNING, "recovery.dispatch_lost", **{id_key: row_id})
        return 0
    try:
        cwd = task_workspace_dir(repository, row_id) if model is Task else review_workspace_dir(repository, row_id)
        await droid.resume_interrupted(session_id, cwd=cwd, on_complete=partial(finalize, row_id))
    except Exception as exc:
        _mark_row_failed(
            model,
            row_id,
            f"session lost after service restart: {safe_error(exc)}",
            stamp_completed=stamp_completed,
        )
        log_event(
            logger,
            logging.WARNING,
            "recovery.session_lost",
            session_id=session_id,
            error=str(exc),
            **{id_key: row_id},
        )
        return 0
    with db_session() as session:
        row = session.get(model, row_id)
        if row is not None:
            row.status = TaskStatus.RUNNING
    return 1


async def recover_interrupted_tasks(droid: DroidClient | None = None) -> int:
    """Resume sessions orphaned by a service restart; fail rows that cannot run.

    Droid sessions persist on disk, so a RUNNING row whose session is no longer
    streaming can be resumed with a continuation prompt. Rows that never got a
    session (dispatch interrupted) are marked failed.
    """
    droid = droid or get_droid_client()

    with db_session() as session:
        fix_rows = session.query(Task).filter(Task.status.in_(TaskStatus.ACTIVE)).all()
        fix_snapshots = [(t.id, t.droid_session_id, t.repository) for t in fix_rows]
        review_rows = session.query(ReviewTask).filter(ReviewTask.status.in_(TaskStatus.ACTIVE)).all()
        review_snapshots = [(r.id, r.droid_session_id, r.repository) for r in review_rows]

    if not droid.configured:
        # Nothing can be resumed, but stored work outside current policy is
        # still refused visibly rather than left looking active.
        _fail_rows_outside_policy(Task, fix_snapshots, stamp_completed=False, id_key="task_id")
        _fail_rows_outside_policy(ReviewTask, review_snapshots, stamp_completed=True, id_key="review_id")
        return 0

    recovered = 0
    for task_id, session_id, repository in fix_snapshots:
        if session_id and session_id in droid.active_sessions:
            continue
        recovered += await _resume_orphaned_row(
            Task,
            task_id,
            session_id,
            repository,
            droid,
            finalize_fix_task,
            stamp_completed=False,
            id_key="task_id",
        )

    for review_id, session_id, repository in review_snapshots:
        if session_id and session_id in droid.active_sessions:
            continue
        recovered += await _resume_orphaned_row(
            ReviewTask,
            review_id,
            session_id,
            repository,
            droid,
            finalize_review_task,
            stamp_completed=True,
            id_key="review_id",
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
