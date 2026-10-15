"""Operator pages (task and review detail) and the executive overview API.

Pages are server-rendered shells; their scripts read the existing protected
JSON APIs. Every route sits behind the default-deny operator access middleware.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from app.database import db_session
from app.economics import portfolio_economics
from app.models import ReviewTask, Task
from app.overview import overview_summary, staged_tasks
from app.ui_templates import page_context, templates

router = APIRouter()


@router.get("/api/overview", response_model=None)
async def overview() -> dict[str, Any]:
    """Delivery-stage counts and the most recent tasks, keeping completion, acceptance, publication, CI and merge apart."""
    with db_session() as session:
        staged = staged_tasks(session)
        return {**overview_summary(session, staged), "economics": portfolio_economics(session, staged)}


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_page(task_id: int, request: Request) -> Response:
    with db_session() as session:
        task = session.get(Task, task_id)
        summary = task.to_dict() if task is not None else None
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        page_context(request, task_id=task_id, task=summary),
        status_code=200 if summary is not None else 404,
    )


@router.get("/reviews/{review_id}", response_class=HTMLResponse)
async def review_page(review_id: int, request: Request) -> Response:
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        summary = review.to_dict() if review is not None else None
    return templates.TemplateResponse(
        request,
        "review_detail.html",
        page_context(request, review_id=review_id, review=summary),
        status_code=200 if summary is not None else 404,
    )
