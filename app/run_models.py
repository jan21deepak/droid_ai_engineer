"""Per-turn run attempts: one row for every Droid turn Forge starts.

A ``Task`` (implementation) or ``ReviewTask`` (restricted review) is the
*parent*; each fix, follow-up, recovery or review turn is a separate
``RunAttempt`` with its own lifecycle, session, model, SHAs and usage. An
attempt is committed before its SDK turn is spawned, and only an active
attempt can be finalized, so a duplicate or stale completion callback changes
nothing. Facts that were never observed stay ``None`` (unknown); rows that
predate attempts are never back-filled with invented attempts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, RedactedOutputMixin
from app.redaction import redact_text, safe_error
from app.timeutil import format_sgt, iso_sgt

PARENT_TASK = "task"
PARENT_REVIEW = "review"


class AttemptKind:
    FIX = "fix"
    FOLLOW_UP = "follow_up"
    RECOVERY = "recovery"
    REVIEW = "review"

    ALL = (FIX, FOLLOW_UP, RECOVERY, REVIEW)


class AttemptStatus:
    # Committed before the SDK turn is spawned.
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # Orphaned by a service restart; never resumed without an explicit continuation.
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"
    # Refused or halted by a spending/limit check.
    BLOCKED = "blocked"

    ACTIVE = (STARTING, RUNNING)
    TERMINAL = (COMPLETED, FAILED, INTERRUPTED, STOPPED, BLOCKED)


class UsageCompleteness:
    """How much of an attempt's usage was actually observed."""

    UNKNOWN = "unknown"
    PARTIAL = "partial"
    COMPLETE = "complete"


class UsageSource:
    """Where an attempt's per-turn usage contribution came from."""

    # The SDK's completed-turn result: the turn's own total, counted once.
    TERMINAL_RESULT = "terminal_result"
    # A terminal result that omitted some categories, which were filled from this
    # turn's attributable snapshots (see the snapshot sources below). Partial.
    TERMINAL_WITH_SNAPSHOT = "terminal_result_with_snapshot"

    TERMINAL = (TERMINAL_RESULT, TERMINAL_WITH_SNAPSHOT)
    # No terminal result; the latest session-cumulative value of each category in
    # a turn that opened a fresh session (nothing earlier in that session to subtract).
    SNAPSHOT_FRESH_SESSION = "snapshot_fresh_session"
    # No terminal result in a resumed session: per category, latest minus first
    # value seen during this turn. A lower bound, since usage before a category's
    # first observation is missed; a category seen only once contributes nothing.
    SNAPSHOT_DELTA_LOWER_BOUND = "snapshot_delta_lower_bound"

    SNAPSHOT = (SNAPSHOT_FRESH_SESSION, SNAPSHOT_DELTA_LOWER_BOUND)
    # Blocked before any SDK turn was spawned: nothing was used.
    NOT_STARTED = "not_started"


class CostStatus:
    """Dollar status of an attempt. Factory credits are never converted implicitly."""

    UNKNOWN = "unknown"
    ESTIMATED = "estimated"
    RECONCILED = "reconciled"


USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "thinking_tokens",
    "factory_credits",
)

SUMMARY_LIMIT = 4000

_ACTIVE_SQL = "status IN ({})".format(", ".join(f"'{s}'" for s in AttemptStatus.ACTIVE))


class RunAttempt(RedactedOutputMixin, Base):
    __tablename__ = "run_attempts"
    __table_args__ = (
        UniqueConstraint("parent_type", "parent_id", "sequence", name="uq_attempt_sequence"),
        # At most one active turn per parent, enforced by the database as well as the code.
        Index(
            "uq_attempt_one_active",
            "parent_type",
            "parent_id",
            unique=True,
            sqlite_where=text(_ACTIVE_SQL),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_type: Mapped[str] = mapped_column(String(16), index=True)
    parent_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=AttemptStatus.STARTING, index=True)
    continued_from_attempt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reported_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Usage categories are kept separate and nullable: None means not observed, never zero.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_creation_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    factory_credits: Mapped[float | None] = mapped_column(Float, nullable=True)
    usage_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    usage_completeness: Mapped[str] = mapped_column(String(16), default=UsageCompleteness.UNKNOWN)
    usage_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Session-cumulative TokenUsageUpdate snapshots seen during this turn, as JSON
    # {"first": {...}, "latest": {...}, "count": n}. Snapshots are not turn charges.
    usage_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Dollars are only ever an explicit estimate (rate + source) or a manually
    # reconciled value with its source; both stay separate and None means unknown.
    estimated_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimate_rate_usd_per_credit: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimate_source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    estimated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    reconciliation_source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Durable stop intent (operator or runtime limit); a stopped attempt never returns to running.
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    @property
    def active(self) -> bool:
        return self.status in AttemptStatus.ACTIVE

    def snapshot_dict(self) -> dict[str, Any] | None:
        if not self.usage_snapshot:
            return None
        try:
            raw = json.loads(self.usage_snapshot)
        except ValueError:
            return None
        if not isinstance(raw, dict):
            return None
        return {
            "semantics": "session_cumulative",
            "first": raw.get("first"),
            "latest": raw.get("latest"),
            "count": raw.get("count"),
            "updated_at": iso_sgt(self.usage_snapshot_at),
        }

    def usage_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {name: getattr(self, name) for name in USAGE_FIELDS}
        values["source"] = self.usage_source
        values["completeness"] = self.usage_completeness or UsageCompleteness.UNKNOWN
        values["recorded_at"] = iso_sgt(self.usage_recorded_at)
        values["snapshot"] = self.snapshot_dict()
        return values

    @property
    def cost_status(self) -> str:
        if self.reconciled_usd is not None:
            return CostStatus.RECONCILED
        if self.estimated_usd is not None:
            return CostStatus.ESTIMATED
        return CostStatus.UNKNOWN

    def cost_dict(self) -> dict[str, Any]:
        return {
            "status": self.cost_status,
            # Forge has no billing connector, so nothing is ever an observed bill.
            "billed_usd": None,
            "estimated_usd": self.estimated_usd,
            "estimate_rate_usd_per_credit": self.estimate_rate_usd_per_credit,
            "estimate_source": self.estimate_source,
            "estimated_at": iso_sgt(self.estimated_at),
            "reconciled_usd": self.reconciled_usd,
            "reconciliation_source": self.reconciliation_source,
            "reconciled_at": iso_sgt(self.reconciled_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_type": self.parent_type,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "sequence": self.sequence,
            "status": self.status,
            "continued_from_attempt_id": self.continued_from_attempt_id,
            "session_id": self.session_id,
            "requested_model": self.requested_model,
            "reported_model": self.reported_model,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "created_at": iso_sgt(self.created_at),
            "created_at_display": format_sgt(self.created_at),
            "started_at": iso_sgt(self.started_at),
            "completed_at": iso_sgt(self.completed_at),
            "duration_seconds": self.duration_seconds,
            "outcome_subtype": self.outcome_subtype,
            "stop_requested_at": iso_sgt(self.stop_requested_at),
            "stop_reason": self.stop_reason,
            "summary": redact_text(self.summary) if self.summary else self.summary,
            "error": safe_error(self.error) if self.error else self.error,
            "usage": self.usage_dict(),
            "cost": self.cost_dict(),
        }


class RunEvent(Base):
    """One bounded, scrubbed public fact in an attempt's operational timeline.

    Payloads are built by ``app.run_events`` from an allowlist of public fields;
    thinking text, tool inputs and raw tool output are never stored.
    """

    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("attempt_id", "sequence", name="uq_run_event_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(Integer, index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.payload) if self.payload else {}
        except ValueError:
            payload = {}
        return {
            "id": self.id,
            "attempt_id": self.attempt_id,
            "sequence": self.sequence,
            "type": self.event_type,
            "created_at": iso_sgt(self.created_at),
            "payload": payload,
        }


__all__ = [
    "PARENT_REVIEW",
    "PARENT_TASK",
    "SUMMARY_LIMIT",
    "USAGE_FIELDS",
    "AttemptKind",
    "AttemptStatus",
    "CostStatus",
    "RunAttempt",
    "RunEvent",
    "UsageCompleteness",
    "UsageSource",
]
