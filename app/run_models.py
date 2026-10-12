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
    TERMINAL_RESULT = "terminal_result"


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

    @property
    def active(self) -> bool:
        return self.status in AttemptStatus.ACTIVE

    def usage_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {name: getattr(self, name) for name in USAGE_FIELDS}
        values["source"] = self.usage_source
        values["completeness"] = self.usage_completeness or UsageCompleteness.UNKNOWN
        values["recorded_at"] = iso_sgt(self.usage_recorded_at)
        return values

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
            "summary": redact_text(self.summary) if self.summary else self.summary,
            "error": safe_error(self.error) if self.error else self.error,
            "usage": self.usage_dict(),
        }


__all__ = [
    "PARENT_REVIEW",
    "PARENT_TASK",
    "SUMMARY_LIMIT",
    "USAGE_FIELDS",
    "AttemptKind",
    "AttemptStatus",
    "RunAttempt",
    "UsageCompleteness",
    "UsageSource",
]
