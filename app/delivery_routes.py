"""Operator delivery APIs: checks, candidate review, approval, publication and remote CI.

Each step is a separate explicit request; none triggers the next. All routes
sit behind the default-deny operator access middleware (cookie writes also
need CSRF and same-origin). Approval and publication name the exact
candidate SHA the operator saw, and the backend rechecks it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.acceptance_checks import start_check_run
from app.auth import operator_context
from app.candidate_review import start_candidate_review
from app.delivery import DeliveryRefusedError, approve_candidate, task_delivery
from app.publishing import publish_candidate
from app.remote_ci import latest_pushed_publication_id, observe_publication_ci

router = APIRouter()

SHA_PATTERN = r"^[0-9a-f]{40}([0-9a-f]{24})?$"


class CandidateRequest(BaseModel):
    candidate_sha: str = Field(
        ..., pattern=SHA_PATTERN, description="Full SHA of the candidate commit shown to the operator"
    )


def _refused(exc: DeliveryRefusedError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, **exc.extra})


@router.get("/api/tasks/{task_id}/delivery", response_model=None)
async def delivery_detail(task_id: int) -> JSONResponse | dict[str, Any]:
    """Candidate, SHA-bound checks/review/approval, destination, publication, remote CI and readiness."""
    state = task_delivery(task_id)
    if state is None:
        return JSONResponse(status_code=404, content={"detail": "task not found"})
    return state


@router.post("/api/tasks/{task_id}/checks", status_code=202, response_model=None)
async def run_checks(task_id: int) -> JSONResponse | dict[str, Any]:
    """Run the configured acceptance checks against the current candidate in a clean checkout."""
    try:
        return await start_check_run(task_id)
    except DeliveryRefusedError as exc:
        return _refused(exc)


@router.post("/api/tasks/{task_id}/candidate-review", status_code=202, response_model=None)
async def review_candidate(task_id: int) -> JSONResponse | dict[str, Any]:
    """Start a fresh restricted (Read/Grep/Glob) review of the current candidate. Nothing is posted to GitHub."""
    try:
        return await start_candidate_review(task_id)
    except DeliveryRefusedError as exc:
        return _refused(exc)


@router.post("/api/tasks/{task_id}/approve", response_model=None)
async def approve(task_id: int, payload: CandidateRequest, request: Request) -> JSONResponse | dict[str, Any]:
    """Approve publication of exactly ``candidate_sha``. Not a GitHub review; grants no merge permission."""
    ctx = operator_context(request)
    try:
        return await approve_candidate(task_id, payload.candidate_sha, approved_via=ctx.method if ctx else "unknown")
    except DeliveryRefusedError as exc:
        return _refused(exc)


@router.post("/api/tasks/{task_id}/publish", response_model=None)
async def publish(task_id: int, payload: CandidateRequest) -> JSONResponse | dict[str, Any]:
    """Push the approved candidate to its allowed branch and create or reuse a draft PR.

    Complete only when the PR is an open, unmerged draft at the approved SHA.
    502 reports a partial publication with every known fact (pushed or not,
    remote head, PR); retrying continues without duplicating the push or PR.
    409 ``publication_in_progress`` or ``task_busy`` means another task change holds the task.
    """
    try:
        result = await publish_candidate(task_id, payload.candidate_sha)
    except DeliveryRefusedError as exc:
        return _refused(exc)
    if not result["published"]:
        return JSONResponse(status_code=502, content=result)
    return result


@router.post("/api/tasks/{task_id}/ci/refresh", response_model=None)
async def refresh_ci(task_id: int) -> JSONResponse | dict[str, Any]:
    """Observe GitHub CI (read-only) for the task's latest pushed publication."""
    if task_delivery(task_id) is None:
        return JSONResponse(status_code=404, content={"detail": "task not found"})
    publication_id = latest_pushed_publication_id(task_id)
    if publication_id is None:
        return JSONResponse(
            status_code=409,
            content={"detail": "not_published", "reason": "remote CI exists only after the candidate is pushed"},
        )
    return {"publication_id": publication_id, "remote_ci": await observe_publication_ci(publication_id)}
