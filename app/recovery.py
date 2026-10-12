"""Restart recovery and explicit continuation of implementation sessions.

Startup never resumes anything: work orphaned by a restart is marked
``interrupted`` (attempt and parent) with the usage it had observed, and
stopped, blocked or finished attempts are left as they are. Continuing a
session is always an explicit operator request (follow-up or recovery) that is
policy-checked and recorded as a new attempt appended after the earlier ones,
which keep their own outcome and usage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial
from typing import Any

from sqlalchemy.orm import Session

from app import worker
from app.attempts import (
    AttemptConflictError,
    AttemptStart,
    active_attempt,
    fail_attempt_start,
    interrupt_attempt,
    latest_attempt,
    mark_attempt_running,
    restore_parent_after_failed_start,
    rollup_parent_usage,
    start_attempt,
)
from app.database import db_session
from app.droid_client import DroidClient, DroidSessionBusyError, get_droid_client, task_workspace_dir
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.policy import repository_decision
from app.redaction import safe_error
from app.run_events import TurnRecorder
from app.run_models import PARENT_REVIEW, PARENT_TASK, AttemptKind, AttemptStatus, RunAttempt
from app.worker import REPOSITORY_ACCESS, policy_denied

logger = logging.getLogger("app.recovery")

REVIEW_INTERRUPTED_ERROR = (
    "review interrupted by service restart; review sessions are never resumed "
    "(reassign the pull request to start a fresh restricted review)"
)
RESTART_INTERRUPTED_ERROR = (
    "interrupted by service restart; not resumed automatically (request an explicit recovery to continue this session)"
)
DISPATCH_INTERRUPTED_ERROR = "dispatch interrupted by service restart"


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------


def _interrupt_orphaned_attempts(
    session: Session, live: set[str], reason: str, *, parent_type: str | None = None, parent_id: int | None = None
) -> int:
    """Mark active attempts whose session is not streaming as interrupted; returns how many."""
    query = session.query(RunAttempt).filter(RunAttempt.status.in_(AttemptStatus.ACTIVE))
    if parent_type is not None:
        query = query.filter(RunAttempt.parent_type == parent_type, RunAttempt.parent_id == parent_id)
    count = 0
    for attempt in query.all():
        if attempt.session_id and attempt.session_id in live:
            continue
        interrupt_attempt(session, attempt, reason)
        count += 1
    return count


def _is_live(session: Session, parent_type: str, parent_id: int, session_id: str | None, live: set[str]) -> bool:
    if session_id and session_id in live:
        return True
    current = active_attempt(session, parent_type, parent_id)
    return bool(current is not None and current.session_id and current.session_id in live)


def _interrupt_orphaned_task(task_id: int, live: set[str]) -> bool:
    """Close one implementation task orphaned by a restart. It is never resumed here."""
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None or task.status not in TaskStatus.ACTIVE:
            return False
        if _is_live(session, PARENT_TASK, task_id, task.droid_session_id, live):
            return False
        denied = policy_denied(REPOSITORY_ACCESS, task.repository, row=f"task_id={task_id}")
        if denied is not None:
            status, reason = TaskStatus.FAILED, f"blocked by repository policy: {denied.reason}"
        elif task.droid_session_id:
            status, reason = TaskStatus.INTERRUPTED, RESTART_INTERRUPTED_ERROR
        else:
            status, reason = TaskStatus.FAILED, DISPATCH_INTERRUPTED_ERROR
        attempts = _interrupt_orphaned_attempts(session, live, reason, parent_type=PARENT_TASK, parent_id=task_id)
        task.status = status
        task.error = reason
        rollup_parent_usage(session, PARENT_TASK, task)
    log_event(
        logger,
        logging.WARNING,
        "recovery.task_interrupted",
        task_id=task_id,
        status=status,
        attempts_interrupted=attempts,
    )
    return True


def _interrupt_orphaned_review(review_id: int, live: set[str]) -> bool:
    """Close an orphaned review. Reviews are never resumed: a resumed session would not keep the tool restriction."""
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is None or review.status not in TaskStatus.ACTIVE:
            return False
        if _is_live(session, PARENT_REVIEW, review_id, review.droid_session_id, live):
            return False
        reason = (
            REVIEW_INTERRUPTED_ERROR if review.droid_session_id else "review dispatch interrupted by service restart"
        )
        _interrupt_orphaned_attempts(session, live, reason, parent_type=PARENT_REVIEW, parent_id=review_id)
        review.status = TaskStatus.FAILED
        review.error = reason
        if review.completed_at is None:
            review.completed_at = datetime.now(UTC)
        rollup_parent_usage(session, PARENT_REVIEW, review)
    log_event(logger, logging.WARNING, "recovery.review_not_resumed", review_id=review_id)
    return True


async def recover_interrupted_tasks(droid: DroidClient | None = None) -> int:
    """Expose work orphaned by a restart without resuming any of it; returns rows interrupted.

    An active implementation task with a saved session becomes ``interrupted``
    and waits for an explicit operator continuation (a new recovery attempt);
    one that never got a session, or whose repository is outside current
    policy, fails. Orphaned reviews fail and can be reassigned. Every orphaned
    active attempt becomes ``interrupted`` with whatever usage it had observed.
    Stopped, blocked and finished attempts are left exactly as they are, and
    no SDK turn is started.
    """
    droid = droid or get_droid_client()
    live = set(droid.active_sessions)

    with db_session() as session:
        task_ids = [t.id for t in session.query(Task).filter(Task.status.in_(TaskStatus.ACTIVE)).all()]
        review_ids = [r.id for r in session.query(ReviewTask).filter(ReviewTask.status.in_(TaskStatus.ACTIVE)).all()]

    interrupted = sum(1 for task_id in task_ids if _interrupt_orphaned_task(task_id, live))
    interrupted += sum(1 for review_id in review_ids if _interrupt_orphaned_review(review_id, live))
    with db_session() as session:
        # Attempts whose parent was no longer active (e.g. finished by PR reconciliation).
        stray = _interrupt_orphaned_attempts(session, live, RESTART_INTERRUPTED_ERROR)

    if task_ids or review_ids or stray:
        log_event(
            logger,
            logging.INFO,
            "recovery.completed",
            interrupted=interrupted,
            fixes=len(task_ids),
            reviews=len(review_ids),
            stray_attempts=stray,
            resumed=0,
        )
    return interrupted


# ---------------------------------------------------------------------------
# Explicit continuation
# ---------------------------------------------------------------------------


class ContinuationRefusedError(Exception):
    """A follow-up or recovery request that was not started, with its HTTP status."""

    def __init__(self, status_code: int, detail: str, **extra: Any):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.extra = extra


_CONTINUATION_KINDS = (AttemptKind.FOLLOW_UP, AttemptKind.RECOVERY)


async def continue_task(task_id: int, kind: str, instruction: str = "") -> dict[str, Any]:
    """Start an explicitly requested follow-up or recovery turn as a new attempt.

    The attempt is committed (and the task marked running) before the SDK
    turn is spawned. A turn that cannot start closes its attempt as failed and
    puts the task back as it was. Raises ``ContinuationRefusedError``.
    """
    if kind not in _CONTINUATION_KINDS:
        raise ValueError(f"not a continuation kind: {kind}")
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise ContinuationRefusedError(404, "task not found")
        session_id, repository, status = task.droid_session_id, task.repository, task.status
        previous = latest_attempt(session, PARENT_TASK, task_id)
        previous_id = previous.id if previous is not None else None

    access = repository_decision(repository)
    if not access.allowed:
        raise ContinuationRefusedError(403, "repository is not approved by policy", reason=access.reason)
    droid = get_droid_client()
    if not droid.configured:
        raise ContinuationRefusedError(503, "Droid is not configured (FACTORY_API_KEY missing)")
    if not session_id:
        raise ContinuationRefusedError(409, "task has no Droid session to continue")
    if kind == AttemptKind.RECOVERY and status == TaskStatus.COMPLETED:
        raise ContinuationRefusedError(409, "task completed; send a follow-up instead of a recovery")
    if droid.session_busy(session_id):
        raise ContinuationRefusedError(409, "the Droid session is still running a turn; retry when it finishes")
    try:
        start = start_attempt(
            PARENT_TASK,
            task_id,
            kind,
            continued_from_attempt_id=previous_id,
            parent_status=TaskStatus.RUNNING,
        )
    except AttemptConflictError as exc:
        raise ContinuationRefusedError(
            409, "task already has an active attempt", active_attempt_id=exc.active_attempt_id
        ) from None

    on_complete = partial(worker.finalize_fix_task, task_id, attempt_id=start.attempt_id)
    cwd = task_workspace_dir(repository, task_id)
    observer = TurnRecorder(start.attempt_id)
    try:
        if kind == AttemptKind.FOLLOW_UP:
            result = await droid.send_follow_up(
                session_id, instruction, repository=repository, cwd=cwd, on_complete=on_complete, observer=observer
            )
        else:
            result = await droid.resume_interrupted(
                session_id, repository=repository, cwd=cwd, on_complete=on_complete, observer=observer
            )
    except DroidSessionBusyError:
        _abandon_start(task_id, start.attempt_id, "session already had a turn in flight", start)
        raise ContinuationRefusedError(
            409, "the Droid session is still running a turn; retry when it finishes"
        ) from None
    except Exception as exc:
        error = f"{kind.replace('_', '-')} failed: {safe_error(exc)}"
        _abandon_start(task_id, start.attempt_id, error, start)
        raise ContinuationRefusedError(502, error) from None

    mark_attempt_running(
        start.attempt_id, session_id=result.get("session_id") or session_id, base_sha=result.get("base_sha")
    )
    log_event(
        logger,
        logging.INFO,
        "task.continuation_started",
        task_id=task_id,
        attempt_id=start.attempt_id,
        kind=kind,
        sequence=start.sequence,
        session_id=session_id,
    )
    return {
        "detail": "accepted",
        "task_id": task_id,
        "attempt_id": start.attempt_id,
        "sequence": start.sequence,
        "kind": kind,
        "session_id": result.get("session_id") or session_id,
        "status": "running",
    }


def _abandon_start(task_id: int, attempt_id: int, error: str, start: AttemptStart) -> None:
    fail_attempt_start(attempt_id, error)
    restore_parent_after_failed_start(PARENT_TASK, task_id, start)


__all__ = [
    "DISPATCH_INTERRUPTED_ERROR",
    "RESTART_INTERRUPTED_ERROR",
    "REVIEW_INTERRUPTED_ERROR",
    "ContinuationRefusedError",
    "continue_task",
    "recover_interrupted_tasks",
]
