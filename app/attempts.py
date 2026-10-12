"""Attempt lifecycle and per-attempt usage storage (see ``app.run_models``).

The rules this module enforces:

- an attempt is committed (``starting``) before its SDK turn is spawned, and a
  parent never has two active attempts;
- only the active attempt named by a completion callback can be finalized, so
  duplicate or stale callbacks create, overwrite and count nothing;
- follow-ups and recoveries append new attempts; earlier attempts keep their
  status, SHAs and usage;
- usage that was not observed stays ``None``; a parent created before attempts
  existed keeps its usage columns as a historical summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import db_session
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus, accounting_basis
from app.redaction import redact_text
from app.run_events import EventType, append_event
from app.run_models import (
    PARENT_REVIEW,
    PARENT_TASK,
    SUMMARY_LIMIT,
    USAGE_FIELDS,
    AttemptStatus,
    RunAttempt,
    UsageCompleteness,
)
from app.usage import (
    ATTRIBUTION_LIMITS,
    apply_configured_estimate,
    apply_snapshot_usage,
    finalize_usage,
    record_terminal_usage,
    sum_known,
    usage_coverage,
    usage_totals,
    usd_summary,
)

logger = logging.getLogger("app.attempts")

ParentRow = Task | ReviewTask


class AttemptConflictError(Exception):
    """The parent already has an active attempt; no second turn may start."""

    def __init__(self, parent_type: str, parent_id: int, active_attempt_id: int | None = None):
        super().__init__(f"{parent_type} {parent_id} already has an active attempt")
        self.parent_type = parent_type
        self.parent_id = parent_id
        self.active_attempt_id = active_attempt_id


@dataclass(frozen=True)
class AttemptStart:
    attempt_id: int
    sequence: int
    previous_status: str | None
    previous_error: str | None


def get_parent(session: Session, parent_type: str, parent_id: int) -> ParentRow | None:
    if parent_type == PARENT_TASK:
        return session.get(Task, parent_id)
    if parent_type == PARENT_REVIEW:
        return session.get(ReviewTask, parent_id)
    raise ValueError(f"unknown parent type: {parent_type}")


def list_attempts(session: Session, parent_type: str, parent_id: int) -> list[RunAttempt]:
    return (
        session.query(RunAttempt)
        .filter(RunAttempt.parent_type == parent_type, RunAttempt.parent_id == parent_id)
        .order_by(RunAttempt.sequence.asc())
        .all()
    )


def active_attempt(session: Session, parent_type: str, parent_id: int) -> RunAttempt | None:
    return (
        session.query(RunAttempt)
        .filter(
            RunAttempt.parent_type == parent_type,
            RunAttempt.parent_id == parent_id,
            RunAttempt.status.in_(AttemptStatus.ACTIVE),
        )
        .first()
    )


def latest_attempt(session: Session, parent_type: str, parent_id: int) -> RunAttempt | None:
    return (
        session.query(RunAttempt)
        .filter(RunAttempt.parent_type == parent_type, RunAttempt.parent_id == parent_id)
        .order_by(RunAttempt.sequence.desc())
        .first()
    )


def start_attempt(
    parent_type: str,
    parent_id: int,
    kind: str,
    *,
    requested_model: str | None = None,
    base_sha: str | None = None,
    head_sha: str | None = None,
    continued_from_attempt_id: int | None = None,
    parent_status: str | None = None,
) -> AttemptStart:
    """Commit a new ``starting`` attempt for the parent, before anything is spawned.

    With ``parent_status`` the parent row moves to that status (and its error is
    cleared) in the same transaction. Raises ``AttemptConflictError`` while the
    parent has an active attempt, and ``LookupError`` if the parent is missing.
    """
    try:
        with db_session() as session:
            parent = get_parent(session, parent_type, parent_id)
            if parent is None:
                raise LookupError(f"{parent_type} {parent_id} not found")
            current = active_attempt(session, parent_type, parent_id)
            if current is not None:
                raise AttemptConflictError(parent_type, parent_id, current.id)
            last = (
                session.query(func.max(RunAttempt.sequence))
                .filter(RunAttempt.parent_type == parent_type, RunAttempt.parent_id == parent_id)
                .scalar()
            )
            attempt = RunAttempt(
                parent_type=parent_type,
                parent_id=parent_id,
                kind=kind,
                sequence=int(last or 0) + 1,
                status=AttemptStatus.STARTING,
                continued_from_attempt_id=continued_from_attempt_id,
                requested_model=requested_model or None,
                base_sha=base_sha or None,
                head_sha=head_sha or None,
                usage_completeness=UsageCompleteness.UNKNOWN,
            )
            session.add(attempt)
            previous_status, previous_error = parent.status, parent.error
            if parent_status is not None:
                parent.status = parent_status
                parent.error = None
            session.flush()
            started = AttemptStart(attempt.id, attempt.sequence, previous_status, previous_error)
    except IntegrityError:
        # Another writer committed an active attempt (or the same sequence) first.
        raise AttemptConflictError(parent_type, parent_id) from None
    log_event(
        logger,
        logging.INFO,
        "attempt.started",
        attempt_id=started.attempt_id,
        parent_type=parent_type,
        parent_id=parent_id,
        kind=kind,
        sequence=started.sequence,
    )
    return started


def set_attempt_running(attempt: RunAttempt, *, session_id: str | None = None, base_sha: str | None = None) -> bool:
    """Record on an attempt row that its turn is underway; refuses an attempt that already ended."""
    if not attempt.active:
        return False
    attempt.status = AttemptStatus.RUNNING
    if attempt.started_at is None:
        attempt.started_at = datetime.now(UTC)
    if session_id and not attempt.session_id:
        attempt.session_id = session_id
    if base_sha and not attempt.base_sha:
        attempt.base_sha = base_sha
    return True


def mark_attempt_running(attempt_id: int, *, session_id: str | None = None, base_sha: str | None = None) -> bool:
    """Record that the turn is underway. Never revives an attempt that already ended."""
    with db_session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        if attempt is None:
            return False
        return set_attempt_running(attempt, session_id=session_id, base_sha=base_sha)


def fail_attempt_start(attempt_id: int, error: str) -> bool:
    """Close an attempt whose turn never started; its usage stays unknown."""
    with db_session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        if attempt is None or not attempt.active:
            return False
        attempt.status = AttemptStatus.FAILED
        attempt.error = error
        attempt.outcome_subtype = "start_failed"
        attempt.completed_at = datetime.now(UTC)
        append_event(session, attempt.id, EventType.START_FAILED, {"error": attempt.error}, lifecycle=True)
        return True


def restore_parent_after_failed_start(parent_type: str, parent_id: int, start: AttemptStart) -> None:
    """Put the parent back as it was when a continuation could not be started."""
    with db_session() as session:
        parent = get_parent(session, parent_type, parent_id)
        if parent is None or parent.status != TaskStatus.RUNNING:
            return
        if active_attempt(session, parent_type, parent_id) is not None:
            return
        parent.status = start.previous_status or TaskStatus.FAILED
        parent.error = start.previous_error


def claim_active_attempt(session: Session, attempt_id: int, parent_type: str, parent_id: int) -> RunAttempt | None:
    """The attempt a completion callback may finalize, or ``None`` when it must be ignored."""
    attempt = session.get(RunAttempt, attempt_id)
    if attempt is None or attempt.parent_type != parent_type or attempt.parent_id != parent_id:
        return None
    return attempt if attempt.active else None


def usage_from_outcome(outcome: dict[str, Any]) -> dict[str, Any] | None:
    """Terminal usage categories reported for one turn, or ``None`` when none was reported."""
    raw = outcome.get("usage")
    if isinstance(raw, dict):
        return {name: raw.get(name) for name in USAGE_FIELDS}
    if outcome.get("credits") is not None:
        return {"factory_credits": outcome["credits"]}
    return None


def finish_attempt(attempt: RunAttempt, *, status: str, outcome: dict[str, Any], summary: str | None) -> None:
    """Apply a terminal outcome to an active attempt (callers claim it first)."""
    attempt.status = status
    attempt.completed_at = datetime.now(UTC)
    subtype = outcome.get("subtype")
    attempt.outcome_subtype = str(subtype)[:64] if subtype else None
    session_id = outcome.get("session_id")
    if isinstance(session_id, str) and session_id and not attempt.session_id:
        attempt.session_id = session_id
    reported = outcome.get("reported_model")
    if isinstance(reported, str) and reported:
        attempt.reported_model = reported[:128]
    if isinstance(outcome.get("duration_seconds"), int | float):
        attempt.duration_seconds = float(outcome["duration_seconds"])
    if summary:
        attempt.summary = redact_text(summary)[:SUMMARY_LIMIT]
    if status == AttemptStatus.FAILED:
        attempt.error = outcome.get("error") or f"Droid run ended with status: {outcome.get('subtype')}"
    finalize_usage(attempt, usage_from_outcome(outcome))


def interrupt_attempt(session: Session, attempt: RunAttempt, reason: str) -> None:
    """Mark an orphaned attempt interrupted, keeping any usage it had already observed."""
    attempt.status = AttemptStatus.INTERRUPTED
    attempt.error = reason
    attempt.completed_at = datetime.now(UTC)
    if attempt.usage_completeness != UsageCompleteness.COMPLETE:
        apply_snapshot_usage(attempt)
        observed = any(getattr(attempt, name) is not None for name in USAGE_FIELDS)
        attempt.usage_completeness = UsageCompleteness.PARTIAL if observed else UsageCompleteness.UNKNOWN
        apply_configured_estimate(attempt)
    append_event(
        session,
        attempt.id,
        EventType.INTERRUPTED,
        {"reason": attempt.error, "usage_completeness": attempt.usage_completeness},
        lifecycle=True,
    )


def record_attempt_candidate(attempt_id: int, head_sha: str | None) -> None:
    """Pin the candidate commit a completed attempt produced to that attempt."""
    if not head_sha:
        return
    with db_session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        if attempt is not None:
            attempt.head_sha = head_sha


def rollup_parent_usage(session: Session, parent_type: str, parent: ParentRow) -> None:
    """Keep a tracked parent's usage columns equal to the sum of its observed attempts.

    Failed, interrupted and partially observed attempts are included. Parents
    that predate attempts are left alone: their columns are history.
    """
    if not parent.attempts_tracked:
        return
    attempts = list_attempts(session, parent_type, parent.id)
    parent.factory_credits = sum_known([a.factory_credits for a in attempts])
    if isinstance(parent, Task):
        tokens = [
            a.input_tokens + a.output_tokens
            for a in attempts
            if a.input_tokens is not None and a.output_tokens is not None
        ]
        parent.estimated_tokens = int(sum(tokens)) if tokens else None


def _counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def parent_accounting(session: Session, parent_type: str, parent: ParentRow) -> dict[str, Any]:
    """Usage known for a parent: per-attempt observations plus any legacy summary, each counted once.

    Failed, interrupted and retried attempts stay in every total; credits that
    were never observed are reported as unknown rather than zero.
    """
    attempts = list_attempts(session, parent_type, parent.id)
    basis = accounting_basis(parent.attempts_tracked)
    history: dict[str, Any] | None = None
    if not parent.attempts_tracked:
        history = {
            "factory_credits": parent.factory_credits,
            "estimated_tokens": parent.estimated_tokens if isinstance(parent, Task) else None,
            "cost_usd": parent.cost_usd,
            "source": "legacy_summary",
            "per_turn": UsageCompleteness.UNKNOWN,
        }
    attempt_credits = sum_known([a.factory_credits for a in attempts])
    history_credits = history["factory_credits"] if history else None
    has_history = history is not None and history_credits is not None
    completeness = [a.usage_completeness or UsageCompleteness.UNKNOWN for a in attempts]
    unsuccessful = [a for a in attempts if a.status != AttemptStatus.COMPLETED]
    return {
        "accounting_basis": basis,
        "attempt_count": len(attempts),
        "attempt_status_counts": _counts([a.status for a in attempts]),
        "attempt_kind_counts": _counts([a.kind for a in attempts]),
        "attempt_factory_credits": attempt_credits,
        "unsuccessful_attempt_factory_credits": sum_known([a.factory_credits for a in unsuccessful]),
        "attempts_with_unknown_usage": completeness.count(UsageCompleteness.UNKNOWN),
        "attempts_with_partial_usage": completeness.count(UsageCompleteness.PARTIAL),
        "usage_totals": usage_totals(attempts),
        "historical_summary": history,
        "observed_factory_credits": sum_known([attempt_credits, history_credits]),
        "coverage": usage_coverage(attempts, has_history),
        "usd": usd_summary(attempts, has_history=history is not None),
        "attribution_limits": list(ATTRIBUTION_LIMITS),
    }


__all__ = [
    "AttemptConflictError",
    "AttemptStart",
    "active_attempt",
    "claim_active_attempt",
    "fail_attempt_start",
    "finish_attempt",
    "get_parent",
    "interrupt_attempt",
    "latest_attempt",
    "list_attempts",
    "mark_attempt_running",
    "parent_accounting",
    "record_attempt_candidate",
    "record_terminal_usage",
    "restore_parent_after_failed_start",
    "rollup_parent_usage",
    "set_attempt_running",
    "start_attempt",
    "usage_from_outcome",
]
