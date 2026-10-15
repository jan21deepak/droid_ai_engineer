"""Operator APIs for outcome economics and its user-entered inputs.

Every route sits behind the default-deny operator access middleware; writes
also need the session CSRF token. Inputs are finite, non-negative and bounded,
and they are stored and returned as user-entered, never as measurements.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.database import db_session
from app.economics import (
    MAX_BASELINE_MINUTES,
    MAX_EFFORT_MINUTES,
    MAX_HOURLY_USD,
    get_assumptions,
    portfolio_economics,
    task_economics,
)
from app.economics_models import ASSUMPTIONS_ROW_ID, EconomicsAssumptions, HumanEffort
from app.models import Task
from app.usage import SOURCE_LIMIT, clean_source

router = APIRouter()


def _minutes(description: str) -> Any:
    return Field(..., ge=0, le=MAX_EFFORT_MINUTES, strict=True, allow_inf_nan=False, description=description)


class HumanEffortRequest(BaseModel):
    review_minutes: float = _minutes("Human minutes spent reviewing this task's change (user-entered)")
    rework_minutes: float = _minutes("Human minutes spent reworking or fixing it (user-entered)")
    note: str = Field("", max_length=SOURCE_LIMIT, description="Optional short note on how the minutes were counted")


class AssumptionsRequest(BaseModel):
    baseline_minutes_per_change: float = Field(
        ...,
        gt=0,
        le=MAX_BASELINE_MINUTES,
        strict=True,
        allow_inf_nan=False,
        description="Assumed human engineering minutes per change without the agent (not measured)",
    )
    loaded_hourly_usd: float | None = Field(
        None,
        ge=0,
        le=MAX_HOURLY_USD,
        strict=True,
        allow_inf_nan=False,
        description="Optional assumed loaded engineering cost per hour, used only to value the capacity scenario",
    )
    source: str = Field(..., min_length=1, max_length=SOURCE_LIMIT, description="Where the assumptions came from")


def _invalid(detail: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": detail})


def _task_missing() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "task not found"})


@router.get("/api/economics", response_model=None)
async def economics() -> dict[str, Any]:
    """Accepted population, observed credits, dollar coverage, cost per accepted change, effort and scenario."""
    with db_session() as session:
        return portfolio_economics(session)


@router.put("/api/economics/assumptions", response_model=None)
async def save_assumptions(payload: AssumptionsRequest) -> JSONResponse | dict[str, Any]:
    """Save the baseline assumptions for the capacity scenario. They are labeled user-entered everywhere."""
    source = clean_source(payload.source)
    if source is None:
        return _invalid("source must be a non-empty label of at most 200 characters")
    with db_session() as session:
        row = get_assumptions(session)
        if row is None:
            row = EconomicsAssumptions(id=ASSUMPTIONS_ROW_ID)
            session.add(row)
        row.baseline_minutes_per_change = payload.baseline_minutes_per_change
        row.loaded_hourly_usd = payload.loaded_hourly_usd
        row.source = source
        row.updated_at = datetime.now(UTC)
        session.flush()
        return {"assumptions": row.to_dict()}


@router.get("/api/tasks/{task_id}/economics", response_model=None)
async def task_economics_detail(task_id: int) -> JSONResponse | dict[str, Any]:
    """One task's acceptance, observed credits (including failed and retried attempts), dollars, time and effort."""
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            return _task_missing()
        return task_economics(session, task)


@router.put("/api/tasks/{task_id}/human-effort", response_model=None)
async def save_human_effort(task_id: int, payload: HumanEffortRequest) -> JSONResponse | dict[str, Any]:
    """Record the human review and rework minutes for one task, replacing any earlier entry."""
    note = " ".join(payload.note.split())
    if note and clean_source(note) is None:
        return _invalid("note must be at most 200 characters")
    with db_session() as session:
        if session.get(Task, task_id) is None:
            return _task_missing()
        row = session.get(HumanEffort, task_id)
        if row is None:
            row = HumanEffort(task_id=task_id)
            session.add(row)
        row.review_minutes = payload.review_minutes
        row.rework_minutes = payload.rework_minutes
        row.note = clean_source(note) or ""
        row.updated_at = datetime.now(UTC)
        session.flush()
        return {"task_id": task_id, "human_effort": row.to_dict()}
