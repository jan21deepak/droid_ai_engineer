"""Delivery facts bound to an exact candidate commit.

A task's candidate (``Task.candidate_sha``) is evaluated by app-run acceptance
checks (``CheckRun``/``CheckResult``) and a restricted review
(``ReviewTask`` with ``review_target="candidate"``). An operator then approves
that exact SHA (``DeliveryApproval``), and only then can the app publish it
(``Publication``). Every row names the SHA it is about, so a newer candidate
never inherits an older one's evidence, approval or publication. Remote CI is
observed on the publication after the push; it is never a prerequisite for it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.redaction import redact, redact_text, safe_error
from app.timeutil import iso_sgt

OUTPUT_LIMIT = 4000


def _now() -> datetime:
    return datetime.now(UTC)


def bounded_output(text: str | None, limit: int = OUTPUT_LIMIT) -> str:
    """The redacted tail of a command's output (failures are usually reported last)."""
    clean = redact_text(text or "")
    return clean if len(clean) <= limit else "…" + clean[-(limit - 1) :]


class CheckStatus:
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    # The check could not be started or its checkout could not be prepared.
    ERROR = "error"
    # The service restarted while the run was in flight; its results are incomplete.
    INTERRUPTED = "interrupted"
    # Not run because an earlier step of the same run errored.
    SKIPPED = "skipped"

    ACTIVE = (QUEUED, RUNNING)


class CheckRun(Base):
    """One execution of every configured acceptance check against one candidate SHA."""

    __tablename__ = "check_runs"
    __table_args__ = (Index("ix_check_runs_task_sha", "task_id", "candidate_sha"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    # The candidate the operator asked to check.
    candidate_sha: Mapped[str] = mapped_column(String(64))
    # What the clean check checkout actually had at HEAD; results only count when it equals candidate_sha.
    evaluated_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=CheckStatus.QUEUED)
    # JSON list, in run order, of each check's ``CheckSpec.identity()`` (name, timeout, spec digest).
    # Runs recorded before that binding hold a list of bare names, which no longer count as evidence.
    configuration: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def _configuration(self) -> list[Any]:
        try:
            entries = json.loads(self.configuration or "[]")
        except ValueError:
            return []
        return entries if isinstance(entries, list) else []

    def configured_names(self) -> list[str]:
        return [str(e.get("name") if isinstance(e, dict) else e) for e in self._configuration()]

    def configured_specs(self) -> list[str] | None:
        """Per-position spec digests, or ``None`` for a names-only (legacy) run."""
        entries = self._configuration()
        digests = [e.get("spec_sha256") for e in entries if isinstance(e, dict)]
        if not entries or len(digests) != len(entries) or not all(isinstance(d, str) and d for d in digests):
            return None
        return [str(d) for d in digests]

    def to_dict(self, results: list[CheckResult] | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "candidate_sha": self.candidate_sha,
            "evaluated_sha": self.evaluated_sha,
            "status": self.status,
            "configured_checks": self.configured_names(),
            # "specification": results are bound to each check's name, argv and timeout; "names_only": legacy.
            "configuration_binding": "names_only" if self.configured_specs() is None else "specification",
            "error": safe_error(self.error) if self.error else None,
            "created_at": iso_sgt(self.created_at),
            "started_at": iso_sgt(self.started_at),
            "finished_at": iso_sgt(self.finished_at),
            "results": [r.to_dict() for r in results or []],
            "executed_by": "app",
        }


class CheckResult(Base):
    """One configured check's outcome within a ``CheckRun``."""

    __tablename__ = "check_results"
    __table_args__ = (UniqueConstraint("run_id", "position", name="uq_check_result_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(128))
    command: Mapped[str] = mapped_column(Text, default="")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default=CheckStatus.QUEUED)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": redact_text(self.command or ""),
            "required": self.required,
            "status": self.status,
            "exit_code": self.exit_code,
            "output_tail": bounded_output(self.output) if self.output else None,
            "started_at": iso_sgt(self.started_at),
            "finished_at": iso_sgt(self.finished_at),
            "duration_seconds": self.duration_seconds,
        }


class DeliveryApproval(Base):
    """An operator's explicit approval to publish one exact candidate SHA.

    This is delivery approval only: it is not a GitHub review and grants no
    merge permission. It authorizes nothing once the task's candidate changes.
    """

    __tablename__ = "delivery_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    candidate_sha: Mapped[str] = mapped_column(String(64))
    # ``Task.candidate_generation`` when approved: any later candidate recording, even of
    # the same SHA after a different one, needs a fresh approval.
    candidate_generation: Mapped[int] = mapped_column(Integer, default=0)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # How the operator was authenticated (session, bearer or local_dev).
    approved_via: Mapped[str] = mapped_column(String(32), default="")
    check_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def to_dict(self, *, current: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "candidate_sha": self.candidate_sha,
            "candidate_generation": self.candidate_generation,
            "approved_at": iso_sgt(self.approved_at),
            "approved_via": self.approved_via,
            "check_run_id": self.check_run_id,
            "review_id": self.review_id,
            "applies_to_current_candidate": current,
            "kind": "delivery_approval",
            "grants_merge": False,
        }


class PublicationStatus:
    """Outcome of the latest publish attempt. Facts an earlier attempt established
    (``pushed``, ``pushed_at``, remote head, PR identity) are kept whatever this says."""

    PUBLISHING = "publishing"
    # Pushed, remote head verified, and the PR is an open, unmerged draft whose head is the approved SHA.
    PUBLISHED = "published"
    # This attempt stopped before pushing (e.g. the PR base could not be determined).
    FAILED = "failed"
    # Stopped at the push boundary: the candidate, its activity, evidence, approval, policy or
    # workspace no longer matched the approval. This attempt made no outward change.
    REFUSED = "refused"
    # This attempt's push was rejected or the remote could not be read; Forge did not update the branch.
    PUSH_FAILED = "push_failed"
    # After the push the remote branch head is not the approved SHA.
    REMOTE_MISMATCH = "remote_mismatch"
    # Pushed and verified, but the pull request could not be created or found.
    PR_FAILED = "pr_failed"
    # A PR exists but its head is not the approved SHA.
    PR_HEAD_MISMATCH = "pr_head_mismatch"
    # The PR at the approved head is not an open, unmerged draft. Forge never converts, reopens or merges it.
    PR_NONCOMPLIANT = "pr_noncompliant"
    # The service restarted mid-publication; known facts are kept and a retry is allowed.
    INTERRUPTED = "interrupted"

    PARTIAL = (PUSH_FAILED, REMOTE_MISMATCH, PR_FAILED, PR_HEAD_MISMATCH, PR_NONCOMPLIANT, INTERRUPTED, FAILED, REFUSED)


def pull_request_state(pr: dict[str, Any]) -> str | None:
    """``merged``, or GitHub's own ``open``/``closed``; ``None`` when the response does not say."""
    if pr.get("merged") or pr.get("merged_at"):
        return "merged"
    state = pr.get("state")
    return state if isinstance(state, str) and state else None


class CIState:
    """Remote CI as observed on GitHub after publication; separate from local acceptance checks."""

    NOT_PUBLISHED = "not_published"
    NOT_OBSERVED = "not_observed"
    NONE_REPORTED = "none_reported"
    PENDING = "pending"
    FAILING = "failing"
    PASSING = "passing"
    UNAVAILABLE = "unavailable"


class Publication(Base):
    """The app-owned push and draft PR for one approved candidate SHA, with what is known of each step."""

    __tablename__ = "publications"
    # One publication per approval: retries reuse the row, a new approval starts a new one.
    __table_args__ = (UniqueConstraint("approval_id", name="uq_publication_approval"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    candidate_sha: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[int] = mapped_column(Integer)
    repository: Mapped[str] = mapped_column(String(255))
    branch: Mapped[str] = mapped_column(String(255))
    base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=PublicationStatus.PUBLISHING)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # None = not attempted yet; True = the remote branch holds the SHA because of (or before) this publication.
    pushed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    push_detail: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pr_draft: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pr_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pr_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pr_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ci_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ci_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ci_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ci_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ci_checks: Mapped[str | None] = mapped_column(Text, nullable=True)

    def ci_dict(self) -> dict[str, Any]:
        try:
            checks = json.loads(self.ci_checks) if self.ci_checks else []
        except ValueError:
            checks = []
        state = self.ci_state or CIState.NOT_OBSERVED
        return {
            "state": state,
            "detail": safe_error(self.ci_detail) if self.ci_detail else None,
            "observed_at": iso_sgt(self.ci_observed_at),
            "observed_sha": self.ci_head_sha,
            # Observations of another commit say nothing about this publication.
            "stale": bool(self.ci_head_sha and self.ci_head_sha != self.candidate_sha),
            "checks": redact(checks) if isinstance(checks, list) else [],
            "source": "github",
            "prerequisite_for_publication": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "candidate_sha": self.candidate_sha,
            "approval_id": self.approval_id,
            "repository": self.repository,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "status": self.status,
            "complete": self.status == PublicationStatus.PUBLISHED,
            "publish_attempts": self.publish_attempts,
            "pushed": self.pushed,
            "push_detail": self.push_detail,
            "pushed_at": iso_sgt(self.pushed_at),
            "remote_head_sha": self.remote_head_sha,
            "remote_head_matches": bool(self.remote_head_sha and self.remote_head_sha == self.candidate_sha),
            "remote_verified_at": iso_sgt(self.remote_verified_at),
            "pull_request": {
                "number": self.pr_number,
                "url": self.pr_url,
                "draft": self.pr_draft,
                "state": self.pr_state,
                "head_sha": self.pr_head_sha,
                "head_matches": bool(self.pr_head_sha and self.pr_head_sha == self.candidate_sha),
                "action": self.pr_action,
                "merged": self.pr_state == "merged",
            },
            "error": safe_error(self.error) if self.error else None,
            "created_at": iso_sgt(self.created_at),
            "updated_at": iso_sgt(self.updated_at),
            "remote_ci": self.ci_dict(),
        }


__all__ = [
    "OUTPUT_LIMIT",
    "CIState",
    "CheckResult",
    "CheckRun",
    "CheckStatus",
    "DeliveryApproval",
    "Publication",
    "PublicationStatus",
    "bounded_output",
    "pull_request_state",
]
