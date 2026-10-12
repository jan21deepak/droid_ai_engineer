"""Durable stop for implementation, review, follow-up and recovery work, plus the runtime watchdog.

A stop is recorded before anything else: the active attempt becomes
``stopped`` (with its reason and whatever usage it had observed) and the
parent becomes ``stopped``, in one transaction. Only then is the SDK asked to
interrupt the turn; the Droid client cancels a turn that does not wind down.
Because the stop is durable, nothing can return the work to running:

- a turn that had not started yet is refused at the admission gate;
- delayed progress only appends timeline events;
- a late completion callback is rejected (the attempt is no longer active) and
  only the usage it reports is retained, once;
- restart recovery leaves stopped work alone, and nothing is resumed unless the
  operator explicitly requests a continuation, which is a new attempt checked
  against current policy and limits.

The runtime watchdog stops any attempt that has been active longer than the
per-turn runtime limit (``DROID_TURN_TIMEOUT_SECONDS``) through the same path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.attempts import active_attempt, get_parent, rollup_parent_usage, stop_attempt
from app.database import db_session
from app.droid_client import get_droid_client
from app.logging_conf import log_event
from app.models import ReviewTask, TaskStatus
from app.run_events import EventType, append_event
from app.run_limits import runtime_limit_seconds
from app.run_models import AttemptStatus, RunAttempt
from app.timeutil import as_utc

logger = logging.getLogger("app.run_control")


class StopReason:
    OPERATOR = "operator"
    RUNTIME_LIMIT = "runtime_limit"


class StopRefusedError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _stop_message(reason: str) -> str:
    if reason == StopReason.RUNTIME_LIMIT:
        return f"stopped: runtime limit of {runtime_limit_seconds():g}s exceeded (DROID_TURN_TIMEOUT_SECONDS)"
    return "stopped by operator"


async def stop_parent(
    parent_type: str,
    parent_id: int,
    *,
    reason: str = StopReason.OPERATOR,
    only_attempt_id: int | None = None,
) -> dict[str, Any]:
    """Durably stop a task's or review's active work, then request SDK interruption.

    Repeating a stop is harmless and reports ``already_stopped``. With
    ``only_attempt_id`` the stop applies only while that attempt is the active
    one (the watchdog never stops a newer attempt). Raises ``StopRefusedError``
    when there is nothing to stop.
    """
    message = _stop_message(reason)
    with db_session() as session:
        parent = get_parent(session, parent_type, parent_id)
        if parent is None:
            raise StopRefusedError(404, f"{parent_type} not found")
        attempt = active_attempt(session, parent_type, parent_id)
        if only_attempt_id is not None and (attempt is None or attempt.id != only_attempt_id):
            raise StopRefusedError(409, "that attempt is no longer active")
        if attempt is None and parent.status not in TaskStatus.ACTIVE:
            if parent.status == TaskStatus.STOPPED:
                return {
                    "detail": "already_stopped",
                    "already_stopped": True,
                    "parent_type": parent_type,
                    "parent_id": parent_id,
                    "status": parent.status,
                }
            raise StopRefusedError(409, f"{parent_type} is {parent.status}; nothing is running")
        attempt_id = attempt.id if attempt is not None else None
        session_id = (attempt.session_id if attempt is not None else None) or parent.droid_session_id
        if attempt is not None:
            stop_attempt(session, attempt, reason, message)
        parent.status = TaskStatus.STOPPED
        parent.error = message
        if isinstance(parent, ReviewTask) and parent.completed_at is None:
            parent.completed_at = datetime.now(UTC)
        rollup_parent_usage(session, parent_type, parent)

    interrupt = await get_droid_client().interrupt_run(session_id)
    if attempt_id is not None:
        with db_session() as session:
            append_event(session, attempt_id, EventType.INTERRUPT_REQUESTED, interrupt, lifecycle=True)
    log_event(
        logger,
        logging.WARNING,
        "run.stopped",
        parent_type=parent_type,
        parent_id=parent_id,
        attempt_id=attempt_id,
        session_id=session_id,
        reason=reason,
        **interrupt,
    )
    return {
        "detail": "stopped",
        "already_stopped": False,
        "parent_type": parent_type,
        "parent_id": parent_id,
        "status": TaskStatus.STOPPED,
        "attempt_id": attempt_id,
        "stop_reason": reason,
        "message": message,
        "interrupt": interrupt,
    }


def _expired(attempt: RunAttempt, now: datetime, limit: timedelta) -> bool:
    began = as_utc(attempt.started_at or attempt.created_at)
    return began is not None and now - began >= limit


async def enforce_runtime_limits(now: datetime | None = None) -> list[int]:
    """Stop every attempt that has been active longer than the runtime limit; returns their ids."""
    current = as_utc(now) or datetime.now(UTC)
    limit = timedelta(seconds=runtime_limit_seconds())
    with db_session() as session:
        expired = [
            (a.parent_type, a.parent_id, a.id)
            for a in session.query(RunAttempt).filter(RunAttempt.status.in_(AttemptStatus.ACTIVE)).all()
            if _expired(a, current, limit)
        ]
    stopped: list[int] = []
    for parent_type, parent_id, attempt_id in expired:
        try:
            await stop_parent(parent_type, parent_id, reason=StopReason.RUNTIME_LIMIT, only_attempt_id=attempt_id)
        except StopRefusedError:
            continue
        stopped.append(attempt_id)
    return stopped


def watchdog_interval_seconds() -> float:
    return min(5.0, max(0.5, runtime_limit_seconds() / 4))


async def runtime_watchdog(
    stop_event: asyncio.Event, *, interval: Callable[[], float] = watchdog_interval_seconds
) -> None:
    while not stop_event.is_set():
        try:
            await enforce_runtime_limits()
        except Exception as exc:
            log_event(logger, logging.ERROR, "run.watchdog_error", error=type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval())


__all__ = [
    "StopReason",
    "StopRefusedError",
    "enforce_runtime_limits",
    "runtime_watchdog",
    "stop_parent",
    "watchdog_interval_seconds",
]
