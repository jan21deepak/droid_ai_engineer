"""SQLAlchemy ORM models and status constants."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.timeutil import format_sgt, iso_sgt


class Base(DeclarativeBase):
    pass


class TaskStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    ACTIVE = (QUEUED, RUNNING)
    ALL = (QUEUED, RUNNING, COMPLETED, FAILED)


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
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
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
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


class Task(Base):
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    def to_dict(self) -> dict:
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
            "summary": self.summary,
            "factory_credits": self.factory_credits,
            "estimated_tokens": self.estimated_tokens,
            "cost_usd": self.cost_usd,
            "created_at": iso_sgt(self.created_at),
            "created_at_display": format_sgt(self.created_at),
            "completed_at": iso_sgt(self.completed_at),
            "completed_at_display": format_sgt(self.completed_at),
            "duration_seconds": self.duration_seconds,
            "kind": "fix",
        }


class SyncedPullRequest(Base):
    """Open, non-draft PRs available for Droid review assignment."""

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
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
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


class ReviewTask(Base):
    """Tracks a Droid review session against a pull request, including auto-merge."""

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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    def to_dict(self) -> dict:
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
            "summary": self.summary,
            "error": self.error,
            "factory_credits": self.factory_credits,
            "cost_usd": self.cost_usd,
            "created_at": iso_sgt(self.created_at),
            "created_at_display": format_sgt(self.created_at),
            "completed_at": iso_sgt(self.completed_at),
            "completed_at_display": format_sgt(self.completed_at),
            "duration_seconds": self.duration_seconds,
            "kind": "review",
        }
