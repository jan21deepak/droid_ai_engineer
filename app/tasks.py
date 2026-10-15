"""Shared task / Droid session dispatch used by webhook and dashboard APIs."""

import json
import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import worker
from app.attempts import check_admission
from app.database import db_session
from app.droid_client import get_droid_client
from app.logging_conf import log_event
from app.models import Task, TaskStatus, WebhookDelivery
from app.policy import repository_decision
from app.run_limits import RunRefusedError
from app.run_models import PARENT_TASK, AttemptKind

logger = logging.getLogger("app.tasks")


class TaskCreateError(Exception):
    def __init__(self, message: str, status_code: int = 400, task_id: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.task_id = task_id


DUPLICATE_DELIVERY = "duplicate: delivery already processed"


def _delivery_receipt(session: Session, delivery_id: str) -> WebhookDelivery | None:
    return session.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == delivery_id).first()


def _record_task(
    *,
    repository: str,
    repository_url: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    labels: list[str] | None,
    delivery_id: str | None,
) -> int:
    """Store the task and its delivery receipt in one transaction; raise for duplicates.

    The receipt's unique key is the cross-process guard: if a concurrent
    identical delivery commits first, this transaction (task included) rolls
    back and nothing is launched.
    """
    duplicate: TaskCreateError | None = None
    try:
        with db_session() as session:
            receipt = _delivery_receipt(session, delivery_id) if delivery_id else None
            if receipt is not None:
                raise TaskCreateError(DUPLICATE_DELIVERY, status_code=200, task_id=receipt.task_id)
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
                task_id = existing.id
                duplicate = TaskCreateError("duplicate: task already exists", status_code=200, task_id=task_id)
            else:
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
            if delivery_id:
                session.add(WebhookDelivery(delivery_id=delivery_id, task_id=task_id))
                session.flush()
    except IntegrityError:
        with db_session() as session:
            receipt = _delivery_receipt(session, delivery_id) if delivery_id else None
            raced_task_id = receipt.task_id if receipt is not None else None
        log_event(logger, logging.INFO, "webhook.delivery_race_lost", task_id=raced_task_id)
        raise TaskCreateError(DUPLICATE_DELIVERY, status_code=200, task_id=raced_task_id) from None
    if duplicate is not None:
        raise duplicate
    return task_id


async def create_and_dispatch_task(
    *,
    repository: str,
    repository_url: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    labels: list[str] | None = None,
    delivery_id: str | None = None,
) -> dict[str, Any]:
    """Persist a Task and launch a local Droid fix session when configured.

    Returns ``{"task_id": int, "session_id": str | None, "status": str}``.
    The Droid session is launched in the background so webhook callers get an
    immediate 202 (GitHub kills webhook deliveries after ~10 seconds).
    Repositories outside current policy are refused before anything is stored.
    ``delivery_id`` (webhook intake only) is recorded durably with the task;
    the same key never launches work again, while callers without that key
    (a new delivery, a dashboard assignment) can still retry a failed issue.
    """
    access = repository_decision(repository)
    if not access.allowed:
        log_event(logger, logging.WARNING, "policy.repository_denied", repo=repository[:200], reason=access.reason)
        raise TaskCreateError(f"repository not approved by policy: {access.reason}", status_code=403)
    task_id = _record_task(
        repository=repository,
        repository_url=repository_url,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        labels=labels,
        delivery_id=delivery_id,
    )

    log_event(
        logger,
        logging.INFO,
        "task.created",
        task_id=task_id,
        repo=repository,
        issue=issue_number,
    )

    droid = get_droid_client()
    if not droid.configured:
        log_event(logger, logging.WARNING, "droid.not_configured", task_id=task_id)
        return {"task_id": task_id, "session_id": None, "status": TaskStatus.QUEUED}

    try:
        check_admission(PARENT_TASK, task_id, AttemptKind.FIX)
    except RunRefusedError as exc:
        return {
            "task_id": task_id,
            "session_id": None,
            "status": TaskStatus.BLOCKED,
            "detail": exc.code,
            "reason": exc.detail,
            "blocked_attempt_id": exc.blocked_attempt_id,
        }

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
