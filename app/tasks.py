"""Shared task / Devin session creation used by webhook and dashboard APIs."""

import json
import logging

from app.database import db_session
from app.devin import DevinClient, build_prompt
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
    """Persist a Task and create a Devin session when configured.

    Returns ``{"task_id": int, "session_id": str | None, "status": str}``.
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

    session_id = None
    status = TaskStatus.QUEUED
    devin = DevinClient()
    if not devin.configured:
        log_event(logger, logging.WARNING, "devin.not_configured", task_id=task_id)
        return {"task_id": task_id, "session_id": None, "status": status}

    prompt = build_prompt(repository_url, issue_number, issue_title, issue_body or "")
    try:
        result = await devin.create_session(
            prompt,
            title=f"{repository}#{issue_number}: {issue_title}"[:120],
        )
        session_id = result.get("session_id")
        with db_session() as session:
            db_task = session.get(Task, task_id)
            db_task.devin_session_id = session_id
            db_task.status = TaskStatus.RUNNING
        status = TaskStatus.RUNNING
        log_event(
            logger, logging.INFO, "devin.session_linked",
            task_id=task_id, session_id=session_id,
        )
    except Exception as exc:
        with db_session() as session:
            db_task = session.get(Task, task_id)
            db_task.status = TaskStatus.FAILED
            db_task.error = f"Devin session creation failed: {exc}"
        log_event(
            logger, logging.ERROR, "devin.session_create_failed",
            task_id=task_id, error=str(exc),
        )
        raise TaskCreateError(
            "Devin session creation failed",
            status_code=502,
            task_id=task_id,
        ) from exc

    return {"task_id": task_id, "session_id": session_id, "status": status}
