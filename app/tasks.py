"""Shared task / Droid session dispatch used by webhook and dashboard APIs."""

import json
import logging

from app import worker
from app.config import get_settings
from app.database import db_session
from app.droid_client import get_droid_client
from app.logging_conf import log_event
from app.models import Task, TaskStatus

logger = logging.getLogger("app.tasks")


class TaskCreateError(Exception):
    def __init__(self, message: str, status_code: int = 400, task_id: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.task_id = task_id


async def create_and_dispatch_task(
    *,
    repository: str,
    repository_url: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    labels: list[str] | None = None,
) -> dict:
    """Persist a Task and launch a local Droid fix session when configured.

    Returns ``{"task_id": int, "session_id": str | None, "status": str}``.
    The Droid session is launched in the background so webhook callers get an
    immediate 202 (GitHub kills webhook deliveries after ~10 seconds).
    """
    with db_session() as session:
        existing = (
            session.query(Task)
            .filter(
                Task.repository == repository,
                Task.issue_number == issue_number,
                Task.status.in_((TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.COMPLETED)),
            )
            .first()
        )
        if existing:
            raise TaskCreateError(
                "duplicate: task already exists",
                status_code=200,
                task_id=existing.id,
            )

        task = Task(
            repository=repository,
            repository_url=repository_url,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body or "",
            labels=json.dumps(labels or []),
            status=TaskStatus.QUEUED,
        )
        session.add(task)
        session.flush()
        task_id = task.id

    log_event(
        logger, logging.INFO, "task.created",
        task_id=task_id, repo=repository, issue=issue_number,
    )

    droid = get_droid_client()
    if not droid.configured:
        log_event(logger, logging.WARNING, "droid.not_configured", task_id=task_id)
        return {"task_id": task_id, "session_id": None, "status": TaskStatus.QUEUED}

    # Launch in the background: workspace clone + session open can take longer
    # than GitHub's webhook delivery window. The task flips to RUNNING (with a
    # session id) as soon as launch_fix_task succeeds.
    worker.spawn_fix_launch(
        task_id,
        repository=repository,
        repository_url=repository_url,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body or "",
    )

    return {
        "task_id": task_id,
        "session_id": None,
        "status": TaskStatus.QUEUED,
        "detail": "launching",
    }
