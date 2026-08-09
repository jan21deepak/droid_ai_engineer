"""Background worker that polls active Devin sessions/reviews and updates the database."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.database import db_session
from app.devin import DevinClient, extract_pull_request_url, map_status, session_has_merged_pr
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.timeutil import format_sgt

logger = logging.getLogger("app.worker")

# How long a failed session stays eligible for re-checking for a late PR.
RECHECK_FAILED_DAYS = 7

# Grace period before an unstarted review that Devin no longer knows about is
# discarded. Devin occasionally reports a queued review and then drops it.
ABANDONED_REVIEW_HOURS = 6


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
        f"- **Devin session ID:** `{task.devin_session_id}`\n"
        f"- **Pull Request:** {task.pull_request_url or 'n/a'}\n"
        f"- **Runtime:** {_format_runtime(task.duration_seconds)}\n"
        f"- **Completed at:** {format_sgt(task.completed_at) or 'n/a'}\n\n"
        f"**Summary of changes:**\n\n{task.summary or 'No summary available.'}"
    )


def _parse_message_timestamp(value) -> float | None:
    """Normalize Devin message timestamps to unix seconds."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    # Some payloads return milliseconds.
    if ts >= 1e12:
        ts /= 1000.0
    return ts


# Gaps longer than this are treated as idle (waiting for a human / suspended),
# not as Devin actively working the issue.
IDLE_GAP_SECONDS = 15 * 60


def session_active_runtime_seconds(messages: list[dict]) -> float | None:
    """Active Devin fix time from the session transcript.

    Wall-clock span from first→last message overcounts badly: sessions often sit
    idle waiting for a human ("Done, try now") or get resumed hours later. This
    sums only the gaps where Devin is the next speaker and the gap is short
    enough to be continuous work.
    """
    stamps: list[tuple[float, str]] = []
    for message in messages:
        ts = _parse_message_timestamp(message.get("created_at") or message.get("timestamp"))
        if ts is None:
            continue
        source = (message.get("source") or message.get("role") or "").lower()
        stamps.append((ts, source))
    if len(stamps) < 2:
        return None
    stamps.sort(key=lambda item: item[0])

    active = 0.0
    for (prev_ts, _), (ts, source) in zip(stamps, stamps[1:]):
        gap = ts - prev_ts
        if gap <= 0:
            continue
        # Next speaker is the human → this gap was waiting on them, not Devin.
        if source in ("user", "human"):
            continue
        if gap > IDLE_GAP_SECONDS:
            continue
        active += gap
    return active if active > 0 else None


# Backwards-compatible name used by older call sites / tests.
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
    # https://github.com/owner/repo/pull/123
    try:
        parts = pr_url.rstrip("/").split("/")
        if "pull" in parts:
            return int(parts[parts.index("pull") + 1])
    except (ValueError, IndexError):
        return None
    return None


def review_runtime_from_github_reviews(
    reviews: list[dict],
    review_created_at: datetime | None = None,
) -> float | None:
    """Active Devin Review time from GitHub PR review submissions.

    The Devin Review API only returns ``created_at`` with no completion timestamp,
    so a review discovered already-finished would otherwise get duration=None and
    vanish from the stats. We measure engagement as the span of Devin bot review
    comments on the PR, falling back to created_at → first comment when there is
    only a single submission.
    """
    bot_times: list[datetime] = []
    for item in reviews:
        login = ((item.get("user") or {}).get("login") or "").lower()
        if "devin" not in login:
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
        # Prefer the earlier of API created_at and first comment as the start.
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
    devin: DevinClient,
    github: GitHubClient | None = None,
) -> None:
    """Track the auto-triggered Devin Review for a completed fix's PR."""
    pr_url = task.pull_request_url
    if not pr_url:
        return
    pr_number = _parse_pr_number(pr_url)
    if not pr_number:
        return

    with db_session() as session:
        existing = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.repository == task.repository,
                ReviewTask.pr_number == pr_number,
            )
            .first()
        )
        if existing:
            return

    status = TaskStatus.QUEUED
    commit_sha = None
    created_at = None
    try:
        data = await devin.get_pr_review(pr_url)
        status = DevinClient.map_review_status(data.get("status"))
        commit_sha = data.get("commit_sha")
        created_at = _parse_iso(data.get("created_at"))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            log_event(
                logger,
                logging.WARNING,
                "review.discover_failed",
                pr_url=pr_url,
                error=str(exc),
            )
        # Devin hasn't published a review for this commit yet. Don't record a
        # phantom row; discovery re-runs on the next poll.
        return
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "review.discover_failed",
            pr_url=pr_url,
            error=str(exc),
        )
        return

    duration = None
    completed_at = None
    if status == TaskStatus.COMPLETED:
        github = github or GitHubClient()
        duration = await compute_review_runtime(
            github, task.repository, pr_number, created_at
        )
        completed_at = datetime.now(timezone.utc)

    with db_session() as session:
        again = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.repository == task.repository,
                ReviewTask.pr_number == pr_number,
            )
            .first()
        )
        if again:
            return
        row = ReviewTask(
            repository=task.repository,
            repository_url=task.repository_url,
            pr_number=pr_number,
            pr_title=task.issue_title or f"PR #{pr_number}",
            pr_url=pr_url,
            commit_sha=commit_sha,
            status=status,
            summary="Tracking auto-triggered Devin Review",
            duration_seconds=duration,
            completed_at=completed_at,
        )
        if created_at:
            row.created_at = created_at
        session.add(row)
    log_event(
        logger,
        logging.INFO,
        "review.tracked",
        repo=task.repository,
        pr=pr_number,
        status=status,
        duration_seconds=duration,
    )


async def _attempt_auto_merge(github: GitHubClient, review: ReviewTask) -> tuple[bool, bool, str | None]:
    """Try to merge immediately; fall back to enabling GitHub auto-merge.

    Returns (merged, auto_merge_enabled, error_message).
    """
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
        # Not mergeable yet (checks / reviews) — enable auto-merge so it lands later.
        if exc.response.status_code in (405, 409, 422):
            enabled = await github.enable_auto_merge(review.repository, review.pr_number)
            if enabled:
                return False, True, None
            return False, False, detail or "merge blocked and auto-merge unavailable"
        return False, False, detail or str(exc)
    except (httpx.HTTPError, RuntimeError) as exc:
        return False, False, str(exc)


async def _discard_abandoned_review(github: GitHubClient, review: ReviewTask) -> bool:
    """Resolve a review row that Devin's API no longer knows about (404).

    Devin sometimes reports a queued review and then drops it, leaving a row that
    counts as active forever. GitHub is the tiebreaker: if the bot did post a
    review the work happened and we complete the row, otherwise the row is a
    phantom and gets removed after a grace period.
    """
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
            db_review.summary = f"Devin Review completed for {review.pr_url}"
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
    devin: DevinClient | None = None, github: GitHubClient | None = None
) -> int:
    """Poll active Devin Review jobs and auto-merge when the review completes."""
    devin = devin or DevinClient()
    github = github or GitHubClient()
    if not devin.configured:
        return 0

    with db_session() as session:
        active = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.status.in_(
                    (
                        TaskStatus.QUEUED,
                        TaskStatus.RUNNING,
                        # Keep watching completed-but-not-yet-merged with auto-merge on
                    )
                )
            )
            .all()
        )
        # Any finished review whose PR hasn't been recorded as merged: the PR
        # may have landed outside this app, so re-check GitHub as source of truth.
        pending_merge = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.status == TaskStatus.COMPLETED,
                ReviewTask.merged.is_(False),
            )
            .all()
        )

    updated = 0

    for review in active:
        try:
            data = await devin.get_pr_review(review.pr_url, review.commit_sha)
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

        new_status = DevinClient.map_review_status(data.get("status"))
        commit_sha = data.get("commit_sha") or review.commit_sha
        if new_status == review.status and commit_sha == review.commit_sha:
            continue

        completed_at = None
        duration = None
        merged = False
        auto_merge = False
        error = None
        summary = None

        if new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            completed_at = datetime.now(timezone.utc)
            started = _parse_iso(data.get("created_at")) or review.created_at
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            observed = None
            if started:
                observed = max((completed_at - started).total_seconds(), 0.0)
            # Prefer GitHub's Devin-bot review span — wall-clock from our poll
            # only captures the last few seconds when we discover an already-done review.
            github_duration = await compute_review_runtime(
                github, review.repository, review.pr_number, started
            )
            duration = github_duration or observed
            if new_status == TaskStatus.COMPLETED:
                summary = f"Devin Review completed for {review.pr_url}"
                merged, auto_merge, error = await _attempt_auto_merge(github, review)
                if error and not merged and not auto_merge:
                    # Review itself succeeded; merge is a secondary step.
                    log_event(
                        logger,
                        logging.WARNING,
                        "review.merge_deferred",
                        review_id=review.id,
                        error=error,
                    )
                    error = None
            else:
                error = f"Devin Review status: {data.get('status')}"

        with db_session() as session:
            db_review = session.get(ReviewTask, review.id)
            old_status = db_review.status
            db_review.status = new_status
            if commit_sha:
                db_review.commit_sha = commit_sha
            if completed_at:
                db_review.completed_at = completed_at
                db_review.duration_seconds = duration
            if summary:
                db_review.summary = summary
            if error:
                db_review.error = error
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

    # Poll GitHub for PRs that have auto-merge enabled but aren't merged yet.
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
            # Closed without merge — leave as completed but not merged.
            continue
        else:
            # Still open: retry a direct merge in case checks just passed.
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
    """Sync each Devin-opened PR's open/closed/merged state from GitHub.

    Delivery metrics read this instead of review rows, so a PR still counts
    correctly even when Devin never published a review for it.
    """
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
            # A merged PR means the fix delivered — don't leave the task "running"
            # just because Devin is still waiting_for_user on the session.
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
    devin: DevinClient,
    github: GitHubClient,
) -> int:
    """Recompute fix/review durations that are missing or still include idle time.

    Safe to call every poll — only writes when the corrected value differs.
    """
    updated = 0

    with db_session() as session:
        fixes = (
            session.query(Task)
            .filter(
                Task.devin_session_id.isnot(None),
                Task.status.in_((TaskStatus.COMPLETED, TaskStatus.FAILED)),
            )
            .all()
        )
        reviews = (
            session.query(ReviewTask)
            .filter(ReviewTask.status == TaskStatus.COMPLETED)
            .all()
        )

    for task in fixes:
        try:
            messages = await devin.get_session_messages(task.devin_session_id)
            duration = session_active_runtime_seconds(messages)
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
        current = float(task.duration_seconds) if task.duration_seconds is not None else None
        if current is not None and abs(current - duration) < 1.0:
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
            old=current,
            new=duration,
        )

    for review in reviews:
        duration = await compute_review_runtime(
            github, review.repository, review.pr_number, review.created_at
        )
        if duration is None:
            continue
        current = float(review.duration_seconds) if review.duration_seconds is not None else None
        if current is not None and abs(current - duration) < 1.0:
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
            old=current,
            new=duration,
        )

    return updated


async def poll_once(devin: DevinClient | None = None, github: GitHubClient | None = None) -> int:
    """Poll all active sessions once. Returns number of tasks updated."""
    devin = devin or DevinClient()
    github = github or GitHubClient()
    if not devin.configured:
        return 0

    recheck_cutoff = datetime.now(timezone.utc) - timedelta(days=RECHECK_FAILED_DAYS)
    with db_session() as session:
        active = (
            session.query(Task)
            .filter(Task.status.in_(TaskStatus.ACTIVE), Task.devin_session_id.isnot(None))
            .all()
        )
        # A session suspended for inactivity can still open a PR afterwards, so
        # recently-failed tasks without one are re-checked rather than written off.
        recoverable = (
            session.query(Task)
            .filter(
                Task.status == TaskStatus.FAILED,
                Task.pull_request_url.is_(None),
                Task.devin_session_id.isnot(None),
                Task.created_at >= recheck_cutoff,
            )
            .all()
        )
        active = active + recoverable

    updated = 0
    if active:
        log_event(logger, logging.INFO, "polling.started", active_sessions=len(active))
        for task in active:
            try:
                session_data = await devin.get_session(task.devin_session_id)
                pr_url = extract_pull_request_url(session_data)
                status_value = session_data.get("status") or session_data.get("status_enum")
                pr_merged = session_has_merged_pr(session_data) or task.pr_state == "merged"
                new_status = map_status(
                    status_value,
                    bool(pr_url) or bool(task.pull_request_url),
                    session_data.get("status_detail"),
                    pr_merged=pr_merged,
                )
                acus = session_data.get("acus_consumed")
                if pr_url is None:
                    pr_url = task.pull_request_url
            except Exception as exc:
                log_event(logger, logging.ERROR, "polling.session_error",
                          task_id=task.id, session_id=task.devin_session_id, error=str(exc))
                continue

            acus_changed = (
                acus is not None
                and (task.acus_consumed is None or float(acus) != float(task.acus_consumed))
            )
            if new_status == task.status and pr_url == task.pull_request_url and not acus_changed:
                continue

            summary = None
            completed_at = None
            duration = None
            cost_usd = None
            settings = get_settings()
            if new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                completed_at = datetime.now(timezone.utc)
                messages = await devin.get_session_messages(task.devin_session_id)
                duration = _session_runtime_seconds(messages)
                if new_status == TaskStatus.COMPLETED:
                    try:
                        summary = await devin.get_session_summary(task.devin_session_id)
                    except Exception as exc:
                        log_event(logger, logging.WARNING, "polling.summary_error",
                                  task_id=task.id, error=str(exc))
                if acus is not None:
                    cost_usd = float(acus) * settings.devin_acu_usd

            with db_session() as session:
                db_task = session.get(Task, task.id)
                old_status = db_task.status
                db_task.status = new_status
                if pr_url:
                    db_task.pull_request_url = pr_url
                if summary:
                    db_task.summary = summary
                if completed_at:
                    db_task.completed_at = completed_at
                    db_task.duration_seconds = duration
                if acus is not None:
                    db_task.acus_consumed = float(acus)
                if cost_usd is not None:
                    db_task.cost_usd = cost_usd
                # Devin v3 does not report tokens. Keep this explicitly unknown.
                db_task.estimated_tokens = None
                session.flush()
                refreshed = db_task

            updated += 1
            log_event(logger, logging.INFO, "task.status_changed", task_id=task.id,
                      session_id=task.devin_session_id, old=old_status, new=new_status)
            log_event(logger, logging.INFO, "db.updated", task_id=task.id, status=new_status)

            if new_status == TaskStatus.COMPLETED:
                log_event(logger, logging.INFO, "task.completed", task_id=task.id,
                          session_id=task.devin_session_id, pr=refreshed.pull_request_url,
                          runtime_seconds=refreshed.duration_seconds)
                await github.post_issue_comment(
                    refreshed.repository, refreshed.issue_number, build_completion_comment(refreshed)
                )
                if refreshed.pull_request_url:
                    await ensure_review_for_pr(refreshed, devin, github)
            elif new_status == TaskStatus.FAILED:
                log_event(logger, logging.WARNING, "task.failed", task_id=task.id,
                          session_id=task.devin_session_id)

        log_event(logger, logging.INFO, "polling.completed", updated=updated)

    # Discover auto-triggered reviews for every fix that has opened a PR. Devin
    # reviews the PR as soon as it exists, which is often long before the
    # session itself leaves the running state.
    with db_session() as session:
        tasks_with_pr = (
            session.query(Task)
            .filter(
                Task.pull_request_url.isnot(None),
                Task.devin_session_id.isnot(None),
            )
            .all()
        )
    for task in tasks_with_pr:
        await ensure_review_for_pr(task, devin, github)

    review_updated = await poll_reviews_once(devin, github)

    # Runs last so merges performed by the review pass are picked up in the
    # same cycle rather than showing as still-open until the next one.
    updated += await refresh_pr_states(github, tasks_with_pr)
    updated += await finalize_merged_running_tasks()

    # Recompute any durations still inflated by idle waits or left blank.
    updated += await backfill_durations(devin, github)

    return updated + review_updated


async def worker_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = settings.poll_interval_seconds
    log_event(logger, logging.INFO, "worker.started", interval_seconds=interval)
    while not stop_event.is_set():
        try:
            await poll_once()
        except Exception as exc:  # never let the loop die
            log_event(logger, logging.ERROR, "worker.iteration_error", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log_event(logger, logging.INFO, "worker.stopped")
