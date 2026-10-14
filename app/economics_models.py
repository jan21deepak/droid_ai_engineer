"""Operator-entered economics inputs: per-task human effort and the baseline scenario assumptions.

Nothing here is measured. Both tables hold what the operator typed in, with
when it was saved, so every screen and export can label it as user-entered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.redaction import redact_text
from app.timeutil import iso_sgt

USER_ENTERED = "user_entered"
ASSUMPTIONS_ROW_ID = 1


def _now() -> datetime:
    return datetime.now(UTC)


class HumanEffort(Base):
    """Human review and rework minutes the operator recorded for one task."""

    __tablename__ = "task_human_effort"

    task_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_minutes: Mapped[float] = mapped_column(Float)
    rework_minutes: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded": True,
            "review_minutes": self.review_minutes,
            "rework_minutes": self.rework_minutes,
            "total_minutes": self.review_minutes + self.rework_minutes,
            "note": redact_text(self.note) if self.note else "",
            "updated_at": iso_sgt(self.updated_at),
            "source": USER_ENTERED,
            "measured": False,
        }


def no_human_effort() -> dict[str, Any]:
    return {
        "recorded": False,
        "review_minutes": None,
        "rework_minutes": None,
        "total_minutes": None,
        "note": "",
        "updated_at": None,
        "source": USER_ENTERED,
        "measured": False,
    }


class EconomicsAssumptions(Base):
    """The single set of baseline assumptions used for the capacity scenario."""

    __tablename__ = "economics_assumptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    baseline_minutes_per_change: Mapped[float] = mapped_column(Float)
    loaded_hourly_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_minutes_per_change": self.baseline_minutes_per_change,
            "loaded_hourly_usd": self.loaded_hourly_usd,
            "source": redact_text(self.source),
            "updated_at": iso_sgt(self.updated_at),
            "kind": USER_ENTERED,
            "measured": False,
        }


__all__ = ["ASSUMPTIONS_ROW_ID", "USER_ENTERED", "EconomicsAssumptions", "HumanEffort", "no_human_effort"]
