"""Restricted pre-publication review of a task's exact local candidate.

The review is a fresh Read/Grep/Glob session over a clean checkout of the
candidate SHA and the complete ``base..candidate`` diff, recorded as a
``ReviewTask`` with ``review_target="candidate"`` and ``pr_number=0``. Nothing
is posted to GitHub: there is no pull request yet, and the verdict is local
evidence for operator approval. The reviewer runs no commands; the app runs
the configured acceptance checks separately.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.database import db_session
from app.delivery import DeliveryRefusedError, candidate_refusal
from app.delivery_git import prepare_candidate_review_context
from app.droid_client import DroidRunError, get_droid_client
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.redaction import safe_error
from app.repos import resolve_agent_launch_config
from app.review_outcomes import REVIEW_TARGET_CANDIDATE
from app.worker import (
    REPOSITORY_ACCESS,
    _fail_review,
    _review_awaits_launch,
    _review_refusal,
    policy_denied,
    run_review_over_context,
)

logger = logging.getLogger("app.candidate_review")


async def start_candidate_review(task_id: int) -> dict[str, Any]:
    """Create a candidate review row for the task's current candidate and launch it in the background."""
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise DeliveryRefusedError(404, "task not found")
        refusal = candidate_refusal(task)
        if refusal is not None:
            raise refusal
        if not task.base_sha:
            raise DeliveryRefusedError(409, "base_unknown", reason="the candidate's base commit was not recorded")
        active = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.task_id == task_id,
                ReviewTask.review_target == REVIEW_TARGET_CANDIDATE,
                ReviewTask.status.in_(TaskStatus.ACTIVE),
            )
            .first()
        )
        if active is not None:
            raise DeliveryRefusedError(409, "review_already_active", review_id=active.id)
        if not get_droid_client().configured:
            raise DeliveryRefusedError(409, "droid_not_configured", reason="Droid is not configured")
        sha = task.candidate_sha or ""
        review = ReviewTask(
            repository=task.repository,
            repository_url=task.repository_url or f"https://github.com/{task.repository}",
            pr_number=0,
            pr_title=f"Candidate {sha[:12]} for task #{task_id}",
            pr_url="",
            status=TaskStatus.QUEUED,
            review_target=REVIEW_TARGET_CANDIDATE,
            task_id=task_id,
        )
        session.add(review)
        session.flush()
        review_id = review.id
        target = (task.repository, review.repository_url, task.base_sha, sha)

    refused = _review_refusal(review_id)
    if refused is not None:
        raise DeliveryRefusedError(
            409, refused.code, reason=refused.detail, review_id=review_id, blocked_attempt_id=refused.blocked_attempt_id
        )
    droid = get_droid_client()
    launch = asyncio.create_task(launch_candidate_review(review_id, task_id, *target))
    try:
        droid.track(f"launch:review-{review_id}", launch)
    except DroidRunError:
        launch.cancel()
        raise DeliveryRefusedError(409, "already_running", review_id=review_id) from None
    log_event(logger, logging.INFO, "review.candidate_started", task_id=task_id, review_id=review_id, sha=sha)
    return {"review_id": review_id, "started": True, "candidate_sha": sha}


async def launch_candidate_review(
    review_id: int, task_id: int, repository: str, repository_url: str, base_sha: str, candidate_sha: str
) -> None:
    if policy_denied(REPOSITORY_ACCESS, repository, review_id=review_id) is not None:
        _fail_review(review_id, "blocked by repository policy")
        return
    if not _review_awaits_launch(review_id) or _review_refusal(review_id) is not None:
        return
    launch = resolve_agent_launch_config(repository=repository, repository_url=repository_url)
    try:
        context = await prepare_candidate_review_context(
            repository=repository,
            task_id=task_id,
            review_id=review_id,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            base_label=launch.get("starting_ref") or "task base",
        )
    except Exception as exc:
        _fail_review(review_id, f"candidate review context could not be prepared: {safe_error(exc)}")
        log_event(logger, logging.ERROR, "review.candidate_context_failed", review_id=review_id, error=str(exc))
        return
    if context.head_sha != candidate_sha:
        _fail_review(review_id, "candidate review checkout did not resolve to the candidate commit")
        return
    await run_review_over_context(
        review_id,
        context,
        repository=repository,
        repository_url=repository_url,
        pr_url="",
        model=launch.get("model"),
    )


__all__ = ["launch_candidate_review", "start_candidate_review"]
