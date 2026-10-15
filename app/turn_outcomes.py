"""Outcome fields of one Droid SDK turn, read from its session and terminal result."""

from __future__ import annotations

from typing import Any

from droid_sdk import Session

from app.models import TaskStatus
from app.run_models import USAGE_FIELDS


def usage_categories(usage: object | None) -> dict[str, Any] | None:
    """A terminal result's usage, category by category; ``None`` when the result carried none."""
    if usage is None:
        return None
    return {name: getattr(usage, name, None) for name in USAGE_FIELDS}


def reported_model(session: Session) -> str | None:
    """The model the opened session reports for itself (may be a router alias such as ``auto``)."""
    try:
        model = session.settings.model
    except Exception:
        return None
    return model if isinstance(model, str) and model else None


def result_outcome(result: Any) -> dict[str, Any]:
    """Outcome fields from a completed turn's terminal result."""
    usage = getattr(result, "usage", None)
    tokens = None
    if usage is not None:
        tokens = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    duration = getattr(result, "duration", None)
    return {
        "success": bool(result.success),
        "subtype": getattr(result, "subtype", ""),
        "text": getattr(result, "text", "") or "",
        "duration_seconds": duration.total_seconds() if duration else None,
        "credits": getattr(usage, "factory_credits", None) if usage else None,
        "tokens": tokens or None,
        "usage": usage_categories(usage),
        "error": None,
    }


def map_outcome_status(outcome: dict[str, Any]) -> str:
    """Map a Droid turn outcome to an internal TaskStatus."""
    return TaskStatus.COMPLETED if outcome.get("success") else TaskStatus.FAILED


__all__ = ["map_outcome_status", "reported_model", "result_outcome", "usage_categories"]
