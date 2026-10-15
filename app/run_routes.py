"""Operator APIs for run attempts: task/review detail, attempt detail and timeline,
explicit dollar estimates/reconciliation, follow-up, recovery, stop and run limits.

All routes sit behind the default-deny operator access middleware. Detail
responses report only recorded facts: an attempt's agent outcome is not an
acceptance, publication, CI result or merge, and unobserved facts are ``null``.
Dollar values are never derived from credits implicitly: an estimate needs an
explicit rate and source, and a reconciliation a supplied amount and source.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.attempts import list_attempts, parent_accounting
from app.database import db_session
from app.delivery import delivery_state
from app.models import ReviewTask, Task
from app.recovery import ContinuationRefusedError, continue_task
from app.run_control import StopReason, StopRefusedError, stop_parent
from app.run_events import event_limits, list_events
from app.run_limits import run_limits_summary
from app.run_models import PARENT_REVIEW, PARENT_TASK, AttemptKind, RunAttempt
from app.usage import MAX_USD, MAX_USD_PER_CREDIT, SOURCE_LIMIT, CostInputError, set_estimate, set_reconciliation

router = APIRouter()


class FollowUpRequest(BaseModel):
    instruction: str = Field(
        ...,
        min_length=1,
        description="Follow-up prompt for the existing Droid session",
    )


class CostEstimateRequest(BaseModel):
    rate_usd_per_credit: float = Field(
        ...,
        gt=0,
        le=MAX_USD_PER_CREDIT,
        strict=True,
        allow_inf_nan=False,
        description="Explicit USD per Factory credit used for this estimate (not a verified billing rate)",
    )
    source: str = Field(..., min_length=1, max_length=SOURCE_LIMIT, description="Where the rate came from")


class CostReconciliationRequest(BaseModel):
    usd: float = Field(
        ...,
        ge=0,
        le=MAX_USD,
        strict=True,
        allow_inf_nan=False,
        description="Manually supplied dollar amount for this attempt (e.g. from an invoice)",
    )
    source: str = Field(..., min_length=1, max_length=SOURCE_LIMIT, description="Where the amount came from")


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


async def _stop(parent_type: str, parent_id: int) -> JSONResponse | dict[str, Any]:
    try:
        return await stop_parent(parent_type, parent_id, reason=StopReason.OPERATOR)
    except StopRefusedError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@router.post("/api/tasks/{task_id}/stop", response_model=None)
async def stop_task(task_id: int) -> JSONResponse | dict[str, Any]:
    """Durably stop the task's active implementation, follow-up or recovery turn.

    The stop is recorded before the SDK is asked to interrupt; a late result
    cannot return the task to running or mark it successful, and a restart
    never resumes it. Repeating the stop is harmless.
    """
    return await _stop(PARENT_TASK, task_id)


@router.post("/api/reviews/{review_id}/stop", response_model=None)
async def stop_review(review_id: int) -> JSONResponse | dict[str, Any]:
    """Durably stop a review that is waiting to start or running."""
    return await _stop(PARENT_REVIEW, review_id)


@router.get("/api/run-limits", response_model=None)
async def run_limits() -> dict[str, Any]:
    """Concurrency, runtime and continuation limits plus the configured spending check.

    Under the ``account_cap`` policy the dollar cap is enforced by the Factory
    account, not by Forge; dollars stay unknown without a verified rate.
    """
    with db_session() as session:
        return run_limits_summary(session)


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
            "delivery": delivery_state(session, task),
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
    """One attempt with its usage, dollar status and bounded operational timeline."""
    with db_session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        if attempt is None:
            return _not_found("attempt")
        return {
            "attempt": attempt.to_dict(),
            "events": [e.to_dict() for e in list_events(session, [attempt_id])],
            "event_limits": event_limits(),
        }


@router.get("/api/tasks/{task_id}/timeline", response_model=None)
async def task_timeline(task_id: int) -> JSONResponse | dict[str, Any]:
    """Every attempt's public events for a task, in attempt then event order."""
    with db_session() as session:
        if session.get(Task, task_id) is None:
            return _not_found("task")
        attempts = {a.id: a for a in list_attempts(session, PARENT_TASK, task_id)}
        events = sorted(
            list_events(session, list(attempts)),
            key=lambda e: (attempts[e.attempt_id].sequence, e.sequence),
        )
        return {
            "task_id": task_id,
            "events": [
                {
                    **e.to_dict(),
                    "attempt_sequence": attempts[e.attempt_id].sequence,
                    "kind": attempts[e.attempt_id].kind,
                }
                for e in events
            ],
            "event_limits": event_limits(),
        }


def _update_cost(attempt_id: int, apply: Any) -> JSONResponse | dict[str, Any]:
    with db_session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        if attempt is None:
            return _not_found("attempt")
        try:
            apply(attempt)
        except CostInputError as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return {"attempt_id": attempt_id, "cost": attempt.cost_dict()}


@router.post("/api/attempts/{attempt_id}/cost-estimate", response_model=None)
async def estimate_attempt_cost(attempt_id: int, payload: CostEstimateRequest) -> JSONResponse | dict[str, Any]:
    """Record an explicit dollar *estimate* for a finished attempt's observed credits.

    Refused (409) while the attempt runs or when no credits were observed; the
    cost then stays unknown. An estimate is never presented as a bill.
    """
    return _update_cost(attempt_id, lambda a: set_estimate(a, payload.rate_usd_per_credit, payload.source))


@router.post("/api/attempts/{attempt_id}/cost-reconciliation", response_model=None)
async def reconcile_attempt_cost(attempt_id: int, payload: CostReconciliationRequest) -> JSONResponse | dict[str, Any]:
    """Record a manually reconciled dollar amount for a finished attempt, kept apart from any estimate."""
    return _update_cost(attempt_id, lambda a: set_reconciliation(a, payload.usd, payload.source))
