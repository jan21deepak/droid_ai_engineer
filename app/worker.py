"""Background worker that polls active Cursor agents/reviews and updates the database."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.cursor_client import CursorClient, extract_agent_branch
from app.database import db_session
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.repos import resolve_agent_launch_config
from app.timeutil import format_sgt

logger = logging.getLogger("app.worker")

RECHECK_FAILED_DAYS = 7
ABANDONED_REVIEW_HOURS = 6
IDLE_GAP_SECONDS = 15 * 60


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
        f"- **Cursor agent ID:** `{task.cursor_agent_id}`\n"
        f"- **Pull Request:** {task.pull_request_url or 'n/a'}\n"
        f"- **Runtime:** {_format_runtime(task.duration_seconds)}\n"
        f"- **Completed at:** {format_sgt(task.completed_at) or 'n/a'}\n\n"
        f"**Summary of changes:**\n\n{task.summary or 'No summary available.'}"
    )


def _parse_message_timestamp(value) -> float | None:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts >= 1e12:
        ts /= 1000.0
    return ts


def session_active_runtime_seconds(messages: list[dict]) -> float | None:
    """Active agent work time from a conversation transcript (best-effort)."""
    stamps: list[tuple[float, str]] = []
    for message in messages:
        ts = _parse_message_timestamp(message.get("created_at") or message.get("timestamp"))
        if ts is None:
            continue
        source = (message.get("source") or message.get("role") or message.get("type") or "").lower()
        stamps.append((ts, source))
    if len(stamps) < 2:
        return None
    stamps.sort(key=lambda item: item[0])

    active = 0.0
    for (prev_ts, _), (ts, source) in zip(stamps, stamps[1:]):
        gap = ts - prev_ts
        if gap <= 0:
            continue
        if source in ("user", "human"):
            continue
        if gap > IDLE_GAP_SECONDS:
            continue
        active += gap
    return active if active > 0 else None


def _session_runtime_seconds(messages: list[dict]) -> float | None:
    return session_active_runtime_seconds(messages)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_pr_number(pr_url: str) -> int | None:
    try:
        parts = pr_url.rstrip("/").split("/")
        if "pull" in parts:
            return int(parts[parts.index("pull") + 1])
    except (ValueError, IndexError):
        return None
    return None


async def ensure_pull_request_for_task(
    task: Task,
    run_data: dict,
    github: GitHubClient,
) -> str | None:
    """Open a same-repo PR with the forge GitHub token after the agent pushes a branch.

    Issue-fix agents run with ``auto_create_pr=False`` so Cursor does not try
    to open a PR against an upstream parent of a fork. The forge PAT opens the
    PR *within* ``task.repository`` (``head=owner:branch``, base = fork default /
    starting_ref) so review + merge tracking can continue.
    """
    if task.pull_request_url:
        return task.pull_request_url

    branch = extract_agent_branch(run_data)
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
        pr_number = int(existing.get("number") or 0) or _parse_pr_number(existing["html_url"])
        if pr_number:
            await github.request_bugbot_review(repository, pr_number)
        return existing["html_url"]

    launch = resolve_agent_launch_config(
        repository=repository,
        repository_url=task.repository_url,
    )
    base = launch.get("starting_ref")
    if not base:
        try:
            meta = await github.get_repository(repository)
            base = meta.get("default_branch") or "main"
        except Exception:
            base = get_settings().cursor_starting_ref or "main"

    title = task.issue_title or f"Fix #{task.issue_number}"
    if task.issue_number and f"#{task.issue_number}" not in title:
        title = f"{title} (#{task.issue_number})"
    body_parts = [
        f"Opened automatically by Cursor Forge as a **same-repo** PR on `{repository}`",
        f"after the Cloud Agent pushed `{branch}` (not against an upstream parent).",
        "",
        f"Fixes #{task.issue_number}." if task.issue_number else "",
        "",
        f"Cursor agent: `{task.cursor_agent_id}`",
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
            error=str(exc),
        )
        return None

    pr_url = pr.get("html_url")
    pr_number = int(pr.get("number") or 0) or _parse_pr_number(pr_url or "")
    if pr_number:
        await github.request_bugbot_review(repository, pr_number)
    return pr_url


def review_runtime_from_github_reviews(
    reviews: list[dict],
    review_created_at: datetime | None = None,
) -> float | None:
    """Active review time from GitHub PR review submissions by Cursor-related bots."""
    bot_times: list[datetime] = []
    for item in reviews:
        login = ((item.get("user") or {}).get("login") or "").lower()
        if "cursor" not in login and "bugbot" not in login:
            continue
        stamp = _parse_iso(item.get("submitted_at"))
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        bot_times.append(stamp)
    if not bot_times:
        return None
    bot_times.sort()
    start = bot_times[0]
    end = bot_times[-1]
    if review_created_at is not None:
        created = review_created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created <= end:
            start = min(start, created)
    duration = (end - start).total_seconds()
    return duration if duration > 0 else None


async def compute_review_runtime(
    github: GitHubClient,
    repository: str,
    pr_number: int,
    review_created_at: datetime | None = None,
) -> float | None:
    try:
        reviews = await github.list_pull_request_reviews(repository, pr_number)
    except httpx.HTTPError as exc:
        log_event(
            logger,
            logging.WARNING,
            "review.runtime_lookup_failed",
            repo=repository,
            pr=pr_number,
            error=str(exc),
        )
        return None
    return review_runtime_from_github_reviews(reviews, review_created_at)


async def ensure_review_for_pr(
    task: Task,
    cursor: CursorClient,
    github: GitHubClient | None = None,
) -> None:
    """Deprecated: forge no longer starts Cursor review agents.

    PR review is handled by Cursor Bugbot via ``request_bugbot_review``.
    Kept as a no-op so older callers/tests that monkeypatch this name still work.
    """
    return


async def _attempt_auto_merge(github: GitHubClient, review: ReviewTask) -> tuple[bool, bool, str | None]:
    """Try to merge immediately; fall back to enabling GitHub auto-merge.

    No-op when ``Settings.review_auto_merge`` is False (default) so Bugbot /
    humans can review before merge.
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


async def _discard_abandoned_review(github: GitHubClient, review: ReviewTask) -> bool:
    """Resolve a review row whose Cursor agent is gone / never started."""
    if review.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
        return False

    created = review.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created if created else timedelta(days=365)
    if age < timedelta(hours=ABANDONED_REVIEW_HOURS):
        return False

    duration = await compute_review_runtime(
        github, review.repository, review.pr_number, created
    )

    with db_session() as session:
        db_review = session.get(ReviewTask, review.id)
        if db_review is None:
            return False
        if duration is not None:
            db_review.status = TaskStatus.COMPLETED
            db_review.duration_seconds = duration
            db_review.completed_at = db_review.completed_at or datetime.now(timezone.utc)
            db_review.summary = f"Cursor review completed for {review.pr_url}"
            resolution = "completed_from_github"
        else:
            session.delete(db_review)
            resolution = "discarded"

    log_event(
        logger,
        logging.INFO,
        "review.abandoned_resolved",
        review_id=review.id,
        pr=review.pr_url,
        resolution=resolution,
    )
    return True


async def poll_reviews_once(
    cursor: CursorClient | None = None, github: GitHubClient | None = None
) -> int:
    """Poll active Cursor review agents and auto-merge when the review completes."""
    cursor = cursor or CursorClient()
    github = github or GitHubClient()
    if not cursor.configured:
        return 0

    with db_session() as session:
        active = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.status.in_((TaskStatus.QUEUED, TaskStatus.RUNNING)),
                ReviewTask.cursor_agent_id.isnot(None),
            )
            .all()
        )
        pending_merge = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.status == TaskStatus.COMPLETED,
                ReviewTask.merged.is_(False),
            )
            .all()
        )

    updated = 0
    settings = get_settings()

    for review in active:
        try:
            status, _, run_data = await cursor.get_agent_status(
                review.cursor_agent_id, review.cursor_run_id
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                if await _discard_abandoned_review(github, review):
                    updated += 1
                continue
            log_event(
                logger,
                logging.ERROR,
                "polling.review_error",
                review_id=review.id,
                error=str(exc),
            )
            continue
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "polling.review_error",
                review_id=review.id,
                error=str(exc),
            )
            continue

        new_status = status
        run_id = run_data.get("id") or review.cursor_run_id
        if new_status == review.status and run_id == review.cursor_run_id:
            continue

        completed_at = None
        duration = None
        merged = False
        auto_merge = False
        error = None
        summary = None
        cost_usd = None

        if new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            completed_at = datetime.now(timezone.utc)
            duration = CursorClient.run_duration_seconds(run_data)
            github_duration = await compute_review_runtime(
                github, review.repository, review.pr_number, review.created_at
            )
            duration = github_duration or duration
            if new_status == TaskStatus.COMPLETED:
                summary = run_data.get("result") or f"Cursor review completed for {review.pr_url}"
                cost_usd = settings.cursor_usd_per_agent_run or None
                if settings.review_auto_merge:
                    merged, auto_merge, error = await _attempt_auto_merge(github, review)
                    if error and not merged and not auto_merge:
                        log_event(
                            logger,
                            logging.WARNING,
                            "review.merge_deferred",
                            review_id=review.id,
                            error=error,
                        )
                        error = None
            else:
                error = f"Cursor review status: {run_data.get('status')}"

        with db_session() as session:
            db_review = session.get(ReviewTask, review.id)
            old_status = db_review.status
            db_review.status = new_status
            if run_id:
                db_review.cursor_run_id = run_id
            if completed_at:
                db_review.completed_at = completed_at
                db_review.duration_seconds = duration
            if summary:
                db_review.summary = summary
            if error:
                db_review.error = error
            if cost_usd is not None:
                db_review.cost_usd = cost_usd
            if merged:
                db_review.merged = True
            if auto_merge:
                db_review.auto_merge_enabled = True

        updated += 1
        log_event(
            logger,
            logging.INFO,
            "review.status_changed",
            review_id=review.id,
            old=old_status,
            new=new_status,
            merged=merged,
            auto_merge=auto_merge,
        )

    for review in pending_merge:
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
        elif get_settings().review_auto_merge:
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


async def refresh_pr_states(github: GitHubClient, tasks: list[Task]) -> int:
    """Sync each Cursor-opened PR's open/closed/merged state from GitHub."""
    updated = 0
    for task in tasks:
        if task.pr_state == "merged":
            continue
        pr_number = _parse_pr_number(task.pull_request_url)
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


async def backfill_durations(
    cursor: CursorClient,
    github: GitHubClient,
) -> int:
    """Fill missing durations from Cursor run durationMs / GitHub reviews."""
    updated = 0

    with db_session() as session:
        fixes = (
            session.query(Task)
            .filter(
                Task.cursor_agent_id.isnot(None),
                Task.status.in_((TaskStatus.COMPLETED, TaskStatus.FAILED)),
                Task.duration_seconds.is_(None),
            )
            .all()
        )
        reviews = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.status == TaskStatus.COMPLETED,
                ReviewTask.duration_seconds.is_(None),
            )
            .all()
        )

    for task in fixes:
        try:
            _, _, run_data = await cursor.get_agent_status(
                task.cursor_agent_id, task.cursor_run_id
            )
            duration = CursorClient.run_duration_seconds(run_data)
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "duration.fix_backfill_failed",
                task_id=task.id,
                error=str(exc),
            )
            continue
        if duration is None:
            continue
        with db_session() as session:
            db_task = session.get(Task, task.id)
            db_task.duration_seconds = duration
        updated += 1
        log_event(
            logger,
            logging.INFO,
            "duration.fix_corrected",
            task_id=task.id,
            new=duration,
        )

    for review in reviews:
        duration = await compute_review_runtime(
            github, review.repository, review.pr_number, review.created_at
        )
        if duration is None and review.cursor_agent_id:
            try:
                _, _, run_data = await cursor.get_agent_status(
                    review.cursor_agent_id, review.cursor_run_id
                )
                duration = CursorClient.run_duration_seconds(run_data)
            except Exception:
                duration = None
        if duration is None:
            continue
        with db_session() as session:
            db_review = session.get(ReviewTask, review.id)
            db_review.duration_seconds = duration
            if not db_review.completed_at:
                db_review.completed_at = datetime.now(timezone.utc)
        updated += 1
        log_event(
            logger,
            logging.INFO,
            "duration.review_corrected",
            review_id=review.id,
            new=duration,
        )

    return updated


async def poll_once(cursor: CursorClient | None = None, github: GitHubClient | None = None) -> int:
    """Poll all active agents once. Returns number of tasks updated."""
    cursor = cursor or CursorClient()
    github = github or GitHubClient()
    if not cursor.configured:
        return 0

    recheck_cutoff = datetime.now(timezone.utc) - timedelta(days=RECHECK_FAILED_DAYS)
    with db_session() as session:
        active = (
            session.query(Task)
            .filter(Task.status.in_(TaskStatus.ACTIVE), Task.cursor_agent_id.isnot(None))
            .all()
        )
        recoverable = (
            session.query(Task)
            .filter(
                Task.status == TaskStatus.FAILED,
                Task.pull_request_url.is_(None),
                Task.cursor_agent_id.isnot(None),
                Task.created_at >= recheck_cutoff,
            )
            .all()
        )
        active = active + recoverable

    updated = 0
    settings = get_settings()
    if active:
        log_event(logger, logging.INFO, "polling.started", active_sessions=len(active))
        for task in active:
            try:
                new_status, pr_url, run_data = await cursor.get_agent_status(
                    task.cursor_agent_id, task.cursor_run_id
                )
                if pr_url is None:
                    pr_url = task.pull_request_url
                # Cursor often pushes the branch but fails PR create (GitHub App
                # collaborator / scope). Fall back to the forge PAT.
                if (
                    not pr_url
                    and new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                ):
                    try:
                        pr_url = await ensure_pull_request_for_task(task, run_data, github)
                        if pr_url and new_status == TaskStatus.FAILED:
                            # Branch + PR means the fix landed; recover the task.
                            new_status = TaskStatus.COMPLETED
                    except Exception as exc:
                        log_event(
                            logger,
                            logging.WARNING,
                            "polling.pr_fallback_error",
                            task_id=task.id,
                            error=str(exc),
                        )
                # Prefer FINISHED semantics; if PR merged on GitHub, complete early.
                if task.pr_state == "merged":
                    new_status = TaskStatus.COMPLETED
                run_id = run_data.get("id") or task.cursor_run_id
            except Exception as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "polling.session_error",
                    task_id=task.id,
                    agent_id=task.cursor_agent_id,
                    error=str(exc),
                )
                continue

            if (
                new_status == task.status
                and pr_url == task.pull_request_url
                and run_id == task.cursor_run_id
            ):
                continue

            summary = None
            completed_at = None
            duration = None
            cost_usd = None
            if new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                completed_at = datetime.now(timezone.utc)
                duration = CursorClient.run_duration_seconds(run_data)
                if new_status == TaskStatus.COMPLETED:
                    summary = run_data.get("result")
                    if not summary:
                        try:
                            summary = await cursor.get_run_summary(
                                task.cursor_agent_id, run_id
                            )
                        except Exception as exc:
                            log_event(
                                logger,
                                logging.WARNING,
                                "polling.summary_error",
                                task_id=task.id,
                                error=str(exc),
                            )
                    cost_usd = settings.cursor_usd_per_agent_run or None

            with db_session() as session:
                db_task = session.get(Task, task.id)
                old_status = db_task.status
                db_task.status = new_status
                if run_id:
                    db_task.cursor_run_id = run_id
                if pr_url:
                    db_task.pull_request_url = pr_url
                if summary:
                    db_task.summary = summary
                if completed_at:
                    db_task.completed_at = completed_at
                    db_task.duration_seconds = duration
                if cost_usd is not None:
                    db_task.cost_usd = cost_usd
                db_task.estimated_tokens = None
                session.flush()
                refreshed = db_task

            updated += 1
            log_event(
                logger,
                logging.INFO,
                "task.status_changed",
                task_id=task.id,
                agent_id=task.cursor_agent_id,
                old=old_status,
                new=new_status,
            )
            log_event(logger, logging.INFO, "db.updated", task_id=task.id, status=new_status)

            if new_status == TaskStatus.COMPLETED:
                log_event(
                    logger,
                    logging.INFO,
                    "task.completed",
                    task_id=task.id,
                    agent_id=task.cursor_agent_id,
                    pr=refreshed.pull_request_url,
                    runtime_seconds=refreshed.duration_seconds,
                )
                await github.post_issue_comment(
                    refreshed.repository,
                    refreshed.issue_number,
                    build_completion_comment(refreshed),
                )
                if refreshed.pull_request_url:
                    pr_number = _parse_pr_number(refreshed.pull_request_url)
                    if pr_number:
                        await github.request_bugbot_review(
                            refreshed.repository, pr_number
                        )
            elif new_status == TaskStatus.FAILED:
                log_event(
                    logger,
                    logging.WARNING,
                    "task.failed",
                    task_id=task.id,
                    agent_id=task.cursor_agent_id,
                )

        log_event(logger, logging.INFO, "polling.completed", updated=updated)

    with db_session() as session:
        tasks_with_pr = (
            session.query(Task)
            .filter(
                Task.pull_request_url.isnot(None),
                Task.cursor_agent_id.isnot(None),
            )
            .all()
        )
    # PR review is Bugbot-only (comment ``bugbot run``); forge no longer starts
    # Cursor Cloud review agents or auto-merges.
    updated += await refresh_pr_states(github, tasks_with_pr)
    updated += await finalize_merged_running_tasks()
    updated += await backfill_durations(cursor, github)

    return updated


async def worker_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = settings.poll_interval_seconds
    log_event(logger, logging.INFO, "worker.started", interval_seconds=interval)
    while not stop_event.is_set():
        try:
            await poll_once()
        except Exception as exc:
            log_event(logger, logging.ERROR, "worker.iteration_error", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log_event(logger, logging.INFO, "worker.stopped")
