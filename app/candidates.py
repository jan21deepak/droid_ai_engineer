"""Local candidate handoff: what a successful agent turn produces.

An implementation, follow-up or recovery turn that succeeds yields a *candidate*:
the commit left in the task workspace. Recording it grants nothing. Pushing,
opening a pull request and any approval are separate, app-owned delivery steps
gated on checks, a restricted review and operator approval bound to this exact
SHA; an agent's success text, ``BRANCH:`` line or PR link cannot stand in for them.
A newer candidate SHA replaces the old one, so evidence bound to the old SHA no
longer matches the current candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.database import db_session
from app.logging_conf import log_event
from app.models import Task
from app.timeutil import format_sgt

logger = logging.getLogger("app.candidates")


class CandidateStatus:
    # A committed change on top of the recorded base, with a clean worktree.
    READY = "ready"
    # Committed change exists, but the worktree also has uncommitted edits.
    DIRTY = "dirty"
    # The agent left no commit beyond the base.
    NO_CHANGES = "no_changes"
    # The workspace or its HEAD could not be inspected.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class WorkspaceState:
    """What the app observed in the task workspace after the turn."""

    head_sha: str | None
    branch: str | None
    dirty: bool


def classify_candidate(state: WorkspaceState, base_sha: str | None) -> str:
    if not state.head_sha:
        return CandidateStatus.UNAVAILABLE
    if base_sha and state.head_sha == base_sha:
        return CandidateStatus.NO_CHANGES
    if state.dirty:
        return CandidateStatus.DIRTY
    return CandidateStatus.READY


def record_candidate(task_id: int, state: WorkspaceState) -> str | None:
    """Persist the candidate facts for a completed turn; returns the candidate status."""
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            return None
        status = classify_candidate(state, task.base_sha)
        previous = task.candidate_sha
        task.candidate_sha = state.head_sha
        task.candidate_branch = state.branch
        task.candidate_status = status
        task.candidate_recorded_at = datetime.now(UTC)
        task.candidate_generation = (task.candidate_generation or 0) + 1
    log_event(
        logger,
        logging.INFO,
        "task.candidate_recorded",
        task_id=task_id,
        status=status,
        sha=state.head_sha,
        branch=state.branch,
        replaced=previous if previous and previous != state.head_sha else None,
    )
    return status


def evidence_applies_to_candidate(task: Task, evidence_sha: str | None) -> bool:
    """Whether checks, a review or an approval bound to ``evidence_sha`` cover the task's current candidate.

    Evidence for an earlier candidate stays recorded (on its attempt) but never
    authorizes a newer one, and nothing authorizes a candidate that is not ready.
    """
    return bool(evidence_sha and evidence_sha == task.candidate_sha and task.candidate_status == CandidateStatus.READY)


def format_runtime(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_candidate_comment(task: Task) -> str:
    """Progress note for the issue. It states plainly that nothing is published yet."""
    sha = (task.candidate_sha or "")[:12] or "n/a"
    return (
        "🧪 **Droid candidate ready for review**\n\n"
        f"- **Droid session:** `{task.droid_session_id}`\n"
        f"- **Candidate commit:** `{sha}` on `{task.candidate_branch or 'n/a'}` ({task.candidate_status or 'unknown'})\n"
        f"- **Agent runtime:** {format_runtime(task.duration_seconds)}\n"
        f"- **Completed at:** {format_sgt(task.completed_at) or 'n/a'}\n\n"
        "Nothing has been pushed and no pull request was opened. Publication requires app-run "
        "checks, a restricted review and operator approval of this exact commit.\n\n"
        f"**Agent summary:**\n\n{task.summary or 'No summary available.'}"
    )


__all__ = [
    "CandidateStatus",
    "WorkspaceState",
    "build_candidate_comment",
    "classify_candidate",
    "evidence_applies_to_candidate",
    "format_runtime",
    "record_candidate",
]
