"""Bounded, scrubbed operational timeline for each attempt (``RunEvent`` rows).

Only an allowlist of public facts is kept: turn start/finish, tool names and
error flags, short assistant-text excerpts, SDK errors, permission outcomes
and session-cumulative usage snapshots. Thinking text, tool inputs, raw tool
output and full transcripts are never stored. Every string is redacted and
clipped, and each attempt keeps at most ``MAX_EVENTS_PER_ATTEMPT`` events,
with room reserved so lifecycle events are recorded even after progress
events hit their cap.

``TurnRecorder`` is the observer the Droid client feeds while a turn streams.
Persistence problems are logged and swallowed: the timeline must never break
or alter a turn.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from droid_sdk import (
    AssistantMessage,
    ErrorEvent,
    PermissionResolved,
    TextBlock,
    TokenUsageUpdate,
    ToolCall,
    ToolResult,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import db_session
from app.logging_conf import log_event
from app.redaction import redact, safe_error
from app.run_models import USAGE_FIELDS, RunAttempt, RunEvent
from app.usage import clean_usage, record_snapshot

logger = logging.getLogger("app.run_events")

MAX_EVENTS_PER_ATTEMPT = 200
# Progress events stop here so lifecycle events always fit under the hard cap.
PROGRESS_EVENT_LIMIT = 190
TEXT_LIMIT = 280
PAYLOAD_LIMIT = 2000
SNAPSHOT_PERSIST_INTERVAL_SECONDS = 5.0


class EventType:
    TURN_STARTED = "turn_started"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PERMISSION = "permission"
    ERROR = "error"
    USAGE = "usage"
    TURN_FINISHED = "turn_finished"
    START_FAILED = "start_failed"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"
    INTERRUPT_REQUESTED = "interrupt_requested"
    BLOCKED = "blocked"
    LATE_USAGE_RETAINED = "late_usage_retained"


def event_limits() -> dict[str, int]:
    return {
        "max_events_per_attempt": MAX_EVENTS_PER_ATTEMPT,
        "progress_event_limit": PROGRESS_EVENT_LIMIT,
        "text_chars": TEXT_LIMIT,
        "payload_chars": PAYLOAD_LIMIT,
    }


def clip(value: object) -> str:
    return safe_error(value, limit=TEXT_LIMIT)


def _payload_json(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    cleaned = redact({k: clip(v) if isinstance(v, str) else v for k, v in payload.items()})
    text = json.dumps(cleaned, sort_keys=True, default=str)
    if len(text) > PAYLOAD_LIMIT:
        return json.dumps({"truncated": True})
    return text


def append_event(
    session: Session,
    attempt_id: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    lifecycle: bool = False,
) -> bool:
    """Append one event to the attempt's timeline; ``False`` when the cap was reached."""
    count, last = (
        session.query(func.count(RunEvent.id), func.max(RunEvent.sequence))
        .filter(RunEvent.attempt_id == attempt_id)
        .one()
    )
    if count >= (MAX_EVENTS_PER_ATTEMPT if lifecycle else PROGRESS_EVENT_LIMIT):
        return False
    session.add(
        RunEvent(
            attempt_id=attempt_id,
            sequence=int(last or 0) + 1,
            event_type=event_type,
            created_at=datetime.now(UTC),
            payload=_payload_json(payload),
        )
    )
    session.flush()
    return True


def list_events(session: Session, attempt_ids: list[int]) -> list[RunEvent]:
    if not attempt_ids:
        return []
    return (
        session.query(RunEvent)
        .filter(RunEvent.attempt_id.in_(attempt_ids))
        .order_by(RunEvent.attempt_id.asc(), RunEvent.sequence.asc())
        .all()
    )


def _assistant_excerpt(message: AssistantMessage) -> str:
    # Only visible text blocks; thinking blocks are never read.
    return " ".join(block.text for block in message.content if isinstance(block, TextBlock) and block.text).strip()


def public_event(message: object) -> tuple[str, dict[str, Any]] | None:
    """The public fact an SDK stream event carries, or ``None`` when nothing public should be kept."""
    if isinstance(message, ToolCall):
        return EventType.TOOL_CALL, {"tool": message.name}
    if isinstance(message, ToolResult):
        payload: dict[str, Any] = {"tool": message.tool_name, "is_error": bool(message.is_error)}
        if message.is_error and isinstance(message.content, str):
            payload["error"] = message.content
        return EventType.TOOL_RESULT, payload
    if isinstance(message, AssistantMessage):
        excerpt = _assistant_excerpt(message)
        return (EventType.ASSISTANT_MESSAGE, {"excerpt": excerpt}) if excerpt else None
    if isinstance(message, ErrorEvent):
        error_type = getattr(message.error_type, "value", message.error_type)
        return EventType.ERROR, {"error_type": str(error_type), "message": message.message}
    if isinstance(message, PermissionResolved):
        outcome = getattr(message.selected_option, "value", message.selected_option)
        return EventType.PERMISSION, {"outcome": str(outcome)}
    return None


def snapshot_from(message: object) -> dict[str, int | float] | None:
    if not isinstance(message, TokenUsageUpdate):
        return None
    return clean_usage({name: getattr(message, name, None) for name in USAGE_FIELDS}) or None


class TurnObserver(Protocol):
    def started(self, kind: str) -> None: ...

    def observe(self, message: object) -> None: ...

    def finished(self, outcome: dict[str, Any]) -> None: ...


class TurnRecorder:
    """Persists one attempt's public stream facts and cumulative usage snapshots."""

    def __init__(
        self,
        attempt_id: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        snapshot_interval: float = SNAPSHOT_PERSIST_INTERVAL_SECONDS,
    ):
        self.attempt_id = attempt_id
        self._clock = clock
        self._interval = snapshot_interval
        self._pending: dict[str, int | float] | None = None
        self._last_persisted: float | None = None
        self.dropped = 0

    def _append(self, event_type: str, payload: dict[str, Any], *, lifecycle: bool = False) -> None:
        try:
            with db_session() as session:
                if not append_event(session, self.attempt_id, event_type, payload, lifecycle=lifecycle):
                    self.dropped += 1
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "run_event.persist_failed",
                attempt_id=self.attempt_id,
                event_type=event_type,
                error=type(exc).__name__,
            )

    def _flush_snapshot(self) -> None:
        snapshot, self._pending = self._pending, None
        if snapshot is None:
            return
        self._last_persisted = self._clock()
        try:
            with db_session() as session:
                attempt = session.get(RunAttempt, self.attempt_id)
                if attempt is None or not record_snapshot(attempt, snapshot):
                    return
                if not append_event(session, self.attempt_id, EventType.USAGE, {"cumulative": snapshot}):
                    self.dropped += 1
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "run_event.snapshot_persist_failed",
                attempt_id=self.attempt_id,
                error=type(exc).__name__,
            )

    def admitted(self) -> bool:
        """Whether the attempt is still active, checked just before the paid turn starts.

        Fails closed: if the attempt cannot be read, no turn is started.
        """
        try:
            with db_session() as session:
                attempt = session.get(RunAttempt, self.attempt_id)
                return attempt is not None and attempt.active
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "run_event.admission_check_failed",
                attempt_id=self.attempt_id,
                error=type(exc).__name__,
            )
            return False

    def started(self, kind: str) -> None:
        self._append(EventType.TURN_STARTED, {"kind": kind}, lifecycle=True)

    def observe(self, message: object) -> None:
        snapshot = snapshot_from(message)
        if snapshot is not None:
            self._pending = snapshot
            if self._last_persisted is None or self._clock() - self._last_persisted >= self._interval:
                self._flush_snapshot()
            return
        mapped = public_event(message)
        if mapped is not None:
            self._append(*mapped)

    def finished(self, outcome: dict[str, Any]) -> None:
        self._flush_snapshot()
        payload: dict[str, Any] = {
            "success": bool(outcome.get("success")),
            "subtype": outcome.get("subtype"),
            "duration_seconds": outcome.get("duration_seconds"),
            "terminal_usage_reported": outcome.get("usage") is not None,
            "events_dropped": self.dropped,
        }
        if outcome.get("error"):
            payload["error"] = outcome["error"]
        self._append(EventType.TURN_FINISHED, payload, lifecycle=True)
        # A stopped turn gets no completion callback that counts usage; keep what it reported, once.
        from app.attempts import retain_late_usage

        try:
            retain_late_usage(self.attempt_id, outcome)
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "run_event.late_usage_failed",
                attempt_id=self.attempt_id,
                error=type(exc).__name__,
            )


__all__ = [
    "MAX_EVENTS_PER_ATTEMPT",
    "PROGRESS_EVENT_LIMIT",
    "EventType",
    "TurnObserver",
    "TurnRecorder",
    "append_event",
    "event_limits",
    "list_events",
    "public_event",
    "snapshot_from",
]
