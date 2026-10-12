"""Operator APIs for assigning existing pull requests to restricted review and listing reviews.

Assigning a review only starts a read-only Droid review session; it never
publishes implementation changes, approves delivery or merges.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.database import db_session
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import Repository, ReviewTask, SyncedPullRequest
from app.policy import repository_decision
from app.redaction import safe_error
from app.review_outcomes import REVIEW_TARGET_CANDIDATE
from app.worker import start_pr_review

logger = logging.getLogger("app.reviews")

router = APIRouter()

REVIEW_LIST_LIMIT = 100

# HTTP status for review starts that were refused before any session launched.
_REFUSAL_STATUS = {
    "repository_not_allowed": 403,
    "droid_not_configured": 503,
    "review_already_active": 409,
}


class AssignPullByNumberRequest(BaseModel):
    repository: str = Field(..., min_length=3, max_length=255, description="Registered repository, owner/name")
    pr_number: int = Field(..., ge=1, le=10_000_000, strict=True, description="Pull request number")


@router.get("/api/reviews", response_model=None)
async def list_reviews(limit: int = 20, target: str = "pull_request") -> dict[str, Any]:
    """Recent reviews, newest first. ``target`` is ``pull_request``, ``candidate`` or ``all``."""
    limit = max(1, min(int(limit or 20), REVIEW_LIST_LIMIT))
    with db_session() as session:
        query = session.query(ReviewTask)
        if target == REVIEW_TARGET_CANDIDATE:
            query = query.filter(ReviewTask.review_target == REVIEW_TARGET_CANDIDATE)
        elif target != "all":
            query = query.filter(
                (ReviewTask.review_target.is_(None)) | (ReviewTask.review_target != REVIEW_TARGET_CANDIDATE)
            )
        rows = query.order_by(ReviewTask.id.desc()).limit(limit).all()
        return {"reviews": [r.to_dict() for r in rows], "limit": limit}


async def _pull_snapshot(repository: str, pr_number: int) -> dict[str, Any] | JSONResponse:
    """The synced PR, or a read-only GitHub lookup when it has not been synced yet."""
    with db_session() as session:
        synced = (
            session.query(SyncedPullRequest)
            .filter(SyncedPullRequest.repository == repository, SyncedPullRequest.pr_number == pr_number)
            .first()
        )
        if synced is not None:
            return synced.to_dict()
    try:
        pull = await GitHubClient().get_pull_request(repository, pr_number)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": "pull_request_not_found",
                    "reason": f"{repository} has no pull request #{pr_number}",
                },
            )
        return JSONResponse(
            status_code=502, content={"detail": "pull_request_lookup_failed", "reason": safe_error(exc)}
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502, content={"detail": "pull_request_lookup_failed", "reason": safe_error(exc)}
        )
    if (pull.get("state") or "open") != "open":
        return JSONResponse(
            status_code=409,
            content={"detail": "pull_request_not_open", "reason": f"pull request #{pr_number} is {pull.get('state')}"},
        )
    return {
        "repository": repository,
        "pr_number": pr_number,
        "title": pull.get("title") or "",
        "html_url": pull.get("html_url") or "",
        "draft": bool(pull.get("draft")),
    }


@router.post("/api/pulls/assign-number", response_model=None)
async def assign_pull_by_number(payload: AssignPullByNumberRequest) -> JSONResponse | dict[str, Any]:
    """Start a restricted review of an existing (open, draft or ready) PR in a registered repository."""
    repository = payload.repository.strip()
    with db_session() as session:
        repo = session.query(Repository).filter(Repository.full_name == repository).first()
        repository_url = repo.url if repo is not None else ""
    if repo is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "repository_not_registered", "reason": f"{repository} is not a registered repository"},
        )
    access = repository_decision(repository)
    if not access.allowed:
        return JSONResponse(status_code=403, content={"detail": "repository_not_allowed", "reason": access.reason})

    pull = await _pull_snapshot(repository, payload.pr_number)
    if isinstance(pull, JSONResponse):
        return pull

    result = await start_pr_review(
        repository=repository,
        repository_url=repository_url,
        pr_number=payload.pr_number,
        pr_url=pull.get("html_url") or "",
        pr_title=pull.get("title") or "",
    )
    log_event(
        logger,
        logging.INFO,
        "droid.review_assigned",
        repo=repository,
        pr=payload.pr_number,
        started=result.get("started"),
        via="number",
    )
    body = {**result, "pr_number": payload.pr_number, "repository": repository, "draft": bool(pull.get("draft"))}
    if not result.get("started"):
        return JSONResponse(status_code=_REFUSAL_STATUS.get(str(result.get("detail")), 409), content=body)
    return body
