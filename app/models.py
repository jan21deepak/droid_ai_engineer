"""SQLAlchemy ORM models and status constants."""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from app.redaction import redact, redact_text, safe_error
from app.review_outcomes import REVIEW_TARGET_PULL_REQUEST, github_outcome_dict
from app.timeutil import format_sgt, iso_sgt


class Base(DeclarativeBase):
    pass


class RedactedOutputMixin:
    """Scrub credentials from agent/tool text before it is stored on a row."""

    @validates("error")
    def _redact_error(self, _key: str, value: str | None) -> str | None:
        return safe_error(value) if value else value

    @validates("summary")
    def _redact_summary(self, _key: str, value: str | None) -> str | None:
        return redact_text(value) if value else value


class TaskStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # Work orphaned by a service restart; it waits for an explicit operator continuation.
    INTERRUPTED = "interrupted"
    # Stopped by the operator or a runtime limit; never restarted without an explicit continuation.
    STOPPED = "stopped"
    # Refused before any paid turn by a run limit or spending check (reason in ``error``).
    BLOCKED = "blocked"

    ACTIVE = (QUEUED, RUNNING)
    ALL = (QUEUED, RUNNING, COMPLETED, FAILED, INTERRUPTED, STOPPED, BLOCKED)


ACCOUNTING_ATTEMPTS = "attempts"
ACCOUNTING_LEGACY_SUMMARY = "legacy_summary"


def accounting_basis(attempts_tracked: bool | None) -> str:
    """Rows created before per-turn attempts keep their usage columns as a historical summary."""
    return ACCOUNTING_ATTEMPTS if attempts_tracked else ACCOUNTING_LEGACY_SUMMARY


class Repository(Base):
    """GitHub repositories registered in the dashboard."""

    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # Optional shell command run in the freshly cloned workspace before the
    # Droid session starts (e.g. "pip install -e .", "npm ci").
    setup_command: Mapped[str] = mapped_column(String(1000), default="")
    # Optional per-repo model id override; empty = global DROID_MODEL ("auto").
    droid_model: Mapped[str] = mapped_column(String(128), default="")
    # Optional starting git ref for fix workspaces when cloning.
    starting_ref: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "full_name": self.full_name,
            "url": self.url,
            "description": self.description,
            "setup_command": self.setup_command or "",
            "droid_model": self.droid_model or "",
            "starting_ref": self.starting_ref or "",
            "created_at": iso_sgt(self.created_at),
            "created_at_display": format_sgt(self.created_at),
        }


class SyncedIssue(Base):
    """Open GitHub issues pulled into the app for manual Droid assignment."""

    __tablename__ = "synced_issues"
    __table_args__ = (UniqueConstraint("repository", "issue_number", name="uq_repo_issue"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(String(255), index=True)
    repository_url: Mapped[str] = mapped_column(String(512), default="")
    issue_number: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32), default="open")
    labels: Mapped[str] = mapped_column(Text, default="[]")
    html_url: Mapped[str] = mapped_column(String(512), default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "repository_url": self.repository_url,
            "issue_number": self.issue_number,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "labels": self.labels,
            "html_url": self.html_url,
            "synced_at": iso_sgt(self.synced_at),
            "synced_at_display": format_sgt(self.synced_at),
        }


class Task(RedactedOutputMixin, Base):
    """One issue-fix run: a local Droid session in a workspace clone."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(String(255), index=True)
    repository_url: Mapped[str] = mapped_column(String(512), default="")
    issue_number: Mapped[int] = mapped_column(Integer, index=True)
    issue_title: Mapped[str] = mapped_column(String(512), default="")
    issue_body: Mapped[str] = mapped_column(Text, default="")
    labels: Mapped[str] = mapped_column(Text, default="[]")
    droid_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.QUEUED, index=True)
    pull_request_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Refreshed from GitHub so delivery metrics don't depend on review rows existing.
    pr_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pr_merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    factory_credits: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Local candidate facts (app.candidates). Nullable without defaults so rows
    # from before these columns existed stay "unknown" rather than invented.
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    candidate_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    candidate_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Incremented on every candidate recording, so an operator approval names the exact
    # recording it saw (NULL on older rows = no candidate recorded since the upgrade).
    candidate_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True for rows whose usage columns are a roll-up of their RunAttempt rows. The
    # additive migration leaves it NULL on older rows, whose usage columns are then a
    # historical summary that new attempts never overwrite or re-count.
    attempts_tracked: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "repository_url": self.repository_url,
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "droid_session_id": self.droid_session_id,
            "droid_session_url": None,
            "status": self.status,
            "pull_request_url": self.pull_request_url,
            "pr_state": self.pr_state,
            "merged": self.pr_state == "merged",
            "pr_merged_at": iso_sgt(self.pr_merged_at),
            "summary": redact_text(self.summary) if self.summary else self.summary,
            "error": safe_error(self.error) if self.error else self.error,
            "factory_credits": self.factory_credits,
            "estimated_tokens": self.estimated_tokens,
            "cost_usd": self.cost_usd,
            "created_at": iso_sgt(self.created_at),
            "created_at_display": format_sgt(self.created_at),
            "completed_at": iso_sgt(self.completed_at),
            "completed_at_display": format_sgt(self.completed_at),
            "duration_seconds": self.duration_seconds,
            "base_sha": self.base_sha,
            "candidate": {
                "sha": self.candidate_sha,
                "branch": self.candidate_branch,
                "status": self.candidate_status,
                "recorded_at": iso_sgt(self.candidate_recorded_at),
                "generation": self.candidate_generation,
            },
            "accounting_basis": accounting_basis(self.attempts_tracked),
            "kind": "fix",
        }


class WebhookDelivery(Base):
    """Receipt for an accepted GitHub delivery (``X-GitHub-Delivery``).

    Written in the same transaction as the task it maps to, before any launch,
    so a redelivery of the same key never schedules work again: not after the
    task failed, and not after a restart. ``task_id`` may name a task created
    by an earlier delivery when this one was accepted as its duplicate.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class SyncedPullRequest(Base):
    """Open PRs (draft or ready) available for Droid review assignment."""

    __tablename__ = "synced_pull_requests"
    __table_args__ = (UniqueConstraint("repository", "pr_number", name="uq_repo_pr"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(String(255), index=True)
    repository_url: Mapped[str] = mapped_column(String(512), default="")
    pr_number: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    html_url: Mapped[str] = mapped_column(String(512), default="")
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    author: Mapped[str] = mapped_column(String(128), default="")
    draft: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "repository_url": self.repository_url,
            "pr_number": self.pr_number,
            "title": self.title,
            "body": self.body,
            "html_url": self.html_url,
            "head_sha": self.head_sha,
            "author": self.author,
            "draft": self.draft,
            "synced_at": iso_sgt(self.synced_at),
            "synced_at_display": format_sgt(self.synced_at),
        }


class ReviewTask(RedactedOutputMixin, Base):
    """Tracks a restricted Droid review session against a pull request.

    ``merged`` / ``auto_merge_enabled`` are observed or historical facts only;
    Forge never merges or enables auto-merge.
    """

    __tablename__ = "review_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(String(255), index=True)
    repository_url: Mapped[str] = mapped_column(String(512), default="")
    pr_number: Mapped[int] = mapped_column(Integer, index=True)
    pr_title: Mapped[str] = mapped_column(String(512), default="")
    pr_url: Mapped[str] = mapped_column(String(512), default="")
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    droid_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.QUEUED, index=True)
    merged: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_merge_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    factory_credits: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Exact review context (app.review_policy); commit_sha is the reviewed head.
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    review_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    findings: Mapped[str | None] = mapped_column(Text, nullable=True)
    # See Task.attempts_tracked.
    attempts_tracked: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    # "candidate" for a pre-publication review of a task's local candidate (pr_number 0,
    # never posted to GitHub), "pull_request" for an assigned PR. NULL = PR review row
    # recorded before this column existed.
    review_target: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # What actually reached GitHub for a PR review, kept apart from the AI verdict:
    # e.g. a self-review APPROVE that GitHub refused and Forge re-posted as COMMENT.
    github_review_event: Mapped[str | None] = mapped_column(String(32), nullable=True)
    github_review_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    github_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "repository_url": self.repository_url,
            "pr_number": self.pr_number,
            "pr_title": self.pr_title,
            "pr_url": self.pr_url,
            "commit_sha": self.commit_sha,
            "droid_session_id": self.droid_session_id,
            "droid_session_url": None,
            "status": self.status,
            "merged": self.merged,
            "auto_merge_enabled": self.auto_merge_enabled,
            "summary": redact_text(self.summary) if self.summary else self.summary,
            "error": safe_error(self.error) if self.error else self.error,
            "factory_credits": self.factory_credits,
            "cost_usd": self.cost_usd,
            "created_at": iso_sgt(self.created_at),
            "created_at_display": format_sgt(self.created_at),
            "completed_at": iso_sgt(self.completed_at),
            "completed_at_display": format_sgt(self.completed_at),
            "duration_seconds": self.duration_seconds,
            "base_sha": self.base_sha,
            "diff_complete": self.diff_complete,
            "review_policy": self.review_policy,
            "verdict": self.verdict,
            "findings": _load_findings(self.findings),
            "accounting_basis": accounting_basis(self.attempts_tracked),
            "kind": "review",
            "review_target": self.review_target or REVIEW_TARGET_PULL_REQUEST,
            "task_id": self.task_id,
            "ai_verdict": self.verdict,
            "github_outcome": github_outcome_dict(
                self.github_review_outcome, self.github_review_event, iso_sgt(self.github_review_at)
            ),
        }


def _load_findings(raw: str | None) -> list[Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return redact(parsed) if isinstance(parsed, list) else None
