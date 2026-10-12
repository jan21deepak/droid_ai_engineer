"""GitHub-side state reconciliation run by the background worker loop.

Every pass re-reads repository policy, so stored tasks for repositories that
are no longer approved are skipped without any GitHub call. Polling only
*observes* PR state: Forge never merges a pull request or enables GitHub
auto-merge, whatever a review verdict, stored flag or setting says.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.database import db_session
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.policy import repository_decision
from app.remote_ci import refresh_published_ci

logger = logging.getLogger("app.worker")


def _poll_allowed(repository: str, **ctx: Any) -> bool:
    decision = repository_decision(repository)
    if not decision.allowed:
        log_event(logger, logging.INFO, "policy.poll_skipped", repo=repository, reason=decision.reason, **ctx)
    return decision.allowed


def parse_pr_number(pr_url: str) -> int | None:
    try:
        parts = pr_url.rstrip("/").split("/")
        if "pull" in parts:
            return int(parts[parts.index("pull") + 1])
    except (ValueError, IndexError):
        return None
    return None


async def refresh_pr_states(github: GitHubClient, tasks: list[Task]) -> int:
    """Sync each Droid-opened PR's open/closed/merged state from GitHub."""
    updated = 0
    for task in tasks:
        if task.pr_state == "merged":
            continue
        pr_number = parse_pr_number(task.pull_request_url or "")
        if not pr_number:
            continue
        if not _poll_allowed(task.repository, task_id=task.id):
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
            assert db_task is not None
            db_task.pr_state = state
            db_task.pr_merged_at = merged_at
            if state == "merged" and db_task.status in TaskStatus.ACTIVE:
                db_task.status = TaskStatus.COMPLETED
                if not db_task.completed_at:
                    db_task.completed_at = datetime.now(UTC)
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
                task.completed_at = datetime.now(UTC)
            updated += 1
            log_event(
                logger,
                logging.INFO,
                "task.completed_via_merged_pr",
                task_id=task.id,
                pr=task.pull_request_url,
            )
    return updated


async def poll_github_state(github: GitHubClient | None = None) -> int:
    """One reconciliation pass over GitHub-side PR / merge state."""
    github = github or GitHubClient()
    updated = 0

    with db_session() as session:
        tasks_with_pr = session.query(Task).filter(Task.pull_request_url.isnot(None)).all()
    updated += await refresh_pr_states(github, tasks_with_pr)
    updated += await finalize_merged_running_tasks()
    # Observation only: remote CI never changes a task's delivery approval or publication.
    await refresh_published_ci(github)

    with db_session() as session:
        pending = (
            session.query(ReviewTask)
            # Candidate reviews (pr_number 0) have no pull request to poll.
            .filter(ReviewTask.status == TaskStatus.COMPLETED, ReviewTask.merged.is_(False), ReviewTask.pr_number > 0)
            .all()
        )

    for review in pending:
        if not _poll_allowed(review.repository, review_id=review.id):
            continue
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
                assert db_review is not None
                db_review.merged = True
                if not db_review.completed_at:
                    db_review.completed_at = datetime.now(UTC)
            updated += 1
            log_event(
                logger,
                logging.INFO,
                "review.pr_merged_observed",
                review_id=review.id,
                pr=review.pr_url,
            )

    return updated


async def worker_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    interval = settings.poll_interval_seconds
    log_event(logger, logging.INFO, "worker.started", interval_seconds=interval)
    while not stop_event.is_set():
        try:
            await poll_github_state()
        except Exception as exc:
            log_event(logger, logging.ERROR, "worker.iteration_error", error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
    log_event(logger, logging.INFO, "worker.stopped")
