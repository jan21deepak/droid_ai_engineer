"""Operator APIs for run attempts: task/review detail, attempt detail, follow-up and recovery.

All routes sit behind the default-deny operator access middleware. Detail
responses report only recorded facts: an attempt's agent outcome is not an
acceptance, publication, CI result or merge, and unobserved facts are ``null``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.attempts import list_attempts, parent_accounting
from app.database import db_session
from app.models import ReviewTask, Task
from app.recovery import ContinuationRefusedError, continue_task
from app.run_models import PARENT_REVIEW, PARENT_TASK, AttemptKind, RunAttempt

router = APIRouter()


class FollowUpRequest(BaseModel):
    instruction: str = Field(
        ...,
        min_length=1,
        description="Follow-up prompt for the existing Droid session",
    )


def _not_found(what: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": f"{what} not found"})


async def _continue(task_id: int, kind: str, instruction: str = "") -> JSONResponse | dict[str, Any]:
    try:
        return await continue_task(task_id, kind, instruction)
    except ContinuationRefusedError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, **exc.extra})


@router.post("/api/tasks/{task_id}/follow-up", response_model=None)
async def follow_up_task(task_id: int, payload: FollowUpRequest) -> JSONResponse | dict[str, Any]:
    """Send a follow-up prompt to the task's Droid session as a new attempt.

    Refused while the task has an active attempt; earlier attempts keep their
    outcome and usage.
    """
    return await _continue(task_id, AttemptKind.FOLLOW_UP, payload.instruction)


@router.post("/api/tasks/{task_id}/recover", response_model=None)
async def recover_task(task_id: int) -> JSONResponse | dict[str, Any]:
    """Explicitly resume an interrupted or failed task session as a new recovery attempt.

    Startup never does this on its own. The interrupted/failed attempt keeps
    its status and usage; the recovery is appended after it.
    """
    return await _continue(task_id, AttemptKind.RECOVERY)


@router.get("/api/tasks/{task_id}", response_model=None)
async def task_detail(task_id: int) -> JSONResponse | dict[str, Any]:
    """One implementation task with its ordered attempts and usage coverage."""
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            return _not_found("task")
        return {
            "task": task.to_dict(),
            "attempts": [a.to_dict() for a in list_attempts(session, PARENT_TASK, task_id)],
            "accounting": parent_accounting(session, PARENT_TASK, task),
        }


@router.get("/api/tasks/{task_id}/attempts", response_model=None)
async def task_attempts(task_id: int) -> JSONResponse | dict[str, Any]:
    with db_session() as session:
        if session.get(Task, task_id) is None:
            return _not_found("task")
        return {"attempts": [a.to_dict() for a in list_attempts(session, PARENT_TASK, task_id)]}


@router.get("/api/reviews/{review_id}", response_model=None)
async def review_detail(review_id: int) -> JSONResponse | dict[str, Any]:
    """One restricted review with its attempt(s) and usage coverage."""
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is None:
            return _not_found("review")
        return {
            "review": review.to_dict(),
            "attempts": [a.to_dict() for a in list_attempts(session, PARENT_REVIEW, review_id)],
            "accounting": parent_accounting(session, PARENT_REVIEW, review),
        }


@router.get("/api/attempts/{attempt_id}", response_model=None)
async def attempt_detail(attempt_id: int) -> JSONResponse | dict[str, Any]:
    with db_session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        if attempt is None:
            return _not_found("attempt")
        return {"attempt": attempt.to_dict()}
