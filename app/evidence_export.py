"""Selected-task evidence export: a bounded, redacted summary of one task's recorded facts.

The export is built only from database facts about the task named by its
integer id: candidate and approved SHAs, attempt lifecycle and usage, check
and review results, publication and remote CI state, and the user-entered
economics inputs. It never reads files, includes other tasks, agent summaries,
assistant text, thinking, tool input/output or check output, and it makes no
GitHub call. The whole document is redacted again before it is returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.attempts import list_attempts
from app.database import db_session
from app.delivery import delivery_state
from app.economics import get_assumptions, task_economics
from app.models import Task
from app.overview import STAGE_LABELS, task_stage
from app.redaction import redact, safe_error
from app.run_events import list_events
from app.run_models import PARENT_TASK, RunAttempt
from app.timeutil import iso_sgt

router = APIRouter()

EXPORT_FORMAT = "droid-forge.task-evidence.v1"
ERROR_LIMIT = 280
EXCLUDED = (
    "other tasks and reviews",
    "agent summaries, assistant text and thinking",
    "tool inputs, raw tool output and full transcripts",
    "acceptance check commands and output",
    "review finding text",
    "credentials and secrets (redacted)",
    "file contents from the workspace or server",
)


def _attempt(attempt: RunAttempt, event_counts: dict[str, int]) -> dict[str, Any]:
    usage = attempt.usage_dict()
    usage.pop("snapshot", None)
    return {
        "id": attempt.id,
        "sequence": attempt.sequence,
        "kind": attempt.kind,
        "status": attempt.status,
        "requested_model": attempt.requested_model,
        "reported_model": attempt.reported_model,
        "base_sha": attempt.base_sha,
        "head_sha": attempt.head_sha,
        "started_at": iso_sgt(attempt.started_at),
        "completed_at": iso_sgt(attempt.completed_at),
        "duration_seconds": attempt.duration_seconds,
        "outcome_subtype": attempt.outcome_subtype,
        "stop_reason": attempt.stop_reason,
        "error": safe_error(attempt.error, limit=ERROR_LIMIT) if attempt.error else None,
        "usage": usage,
        "cost": attempt.cost_dict(),
        "timeline_event_counts": event_counts,
    }


def _checks(checks: dict[str, Any]) -> dict[str, Any]:
    run = checks["latest_run"]
    return {
        "status": checks["status"],
        "passed": checks["passed"],
        "candidate_sha": checks["candidate_sha"],
        "executed_by": checks["executed_by"],
        "latest_run": None
        if run is None
        else {
            "id": run["id"],
            "candidate_sha": run["candidate_sha"],
            "evaluated_sha": run["evaluated_sha"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "results": [
                {k: r[k] for k in ("name", "required", "status", "exit_code", "duration_seconds")}
                for r in run["results"]
            ],
        },
    }


def _review(review: dict[str, Any]) -> dict[str, Any]:
    latest = review["latest"]
    return {
        "status": review["status"],
        "approving": review["approving"],
        "tools": review["tools"],
        "latest": None
        if latest is None
        else {
            "id": latest["id"],
            "link": f"/reviews/{int(latest['id'])}",
            "status": latest["status"],
            "reviewed_sha": latest["commit_sha"],
            "base_sha": latest["base_sha"],
            "diff_complete": latest["diff_complete"],
            "review_policy": latest["review_policy"],
            "ai_verdict": latest["ai_verdict"],
            "findings_count": len(latest["findings"] or []),
            "completed_at": latest["completed_at"],
            "github_outcome": latest["github_outcome"]["kind"],
        },
    }


def _age_seconds(observed_at: str | None, now: datetime) -> float | None:
    if not observed_at:
        return None
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError:
        return None
    return max((now - observed).total_seconds(), 0.0)


def _publication(delivery: dict[str, Any], now: datetime) -> dict[str, Any]:
    pub = delivery["publication"] or delivery["latest_publication"]
    ci = delivery["remote_ci"]
    remote_ci = {
        "state": ci["state"],
        "observed_at": ci["observed_at"],
        "observed_sha": ci["observed_sha"],
        "stale": ci["stale"],
        "observed_age_seconds": _age_seconds(ci["observed_at"], now),
        "checks": [
            {k: c.get(k) for k in ("name", "kind", "status", "conclusion", "url")}
            for c in ci["checks"]
            if isinstance(c, dict)
        ],
        "source": ci["source"],
    }
    if pub is None:
        return {"state": "not_published", "publication": None, "remote_ci": remote_ci}
    pull = pub["pull_request"]
    return {
        "state": pub["status"],
        "publication": {
            "id": pub["id"],
            "candidate_sha": pub["candidate_sha"],
            "approval_id": pub["approval_id"],
            "repository": pub["repository"],
            "branch": pub["branch"],
            "base_branch": pub["base_branch"],
            "complete": pub["complete"],
            "pushed": pub["pushed"],
            "push_detail": pub["push_detail"],
            "remote_head_sha": pub["remote_head_sha"],
            "remote_head_matches": pub["remote_head_matches"],
            "pull_request": {k: pull[k] for k in ("number", "url", "draft", "state", "head_sha", "head_matches")},
            "updated_at": pub["updated_at"],
        },
        "remote_ci": remote_ci,
    }


def task_evidence(task_id: int) -> dict[str, Any] | None:
    now = datetime.now(UTC)
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            return None
        delivery = delivery_state(session, task)
        stage = task_stage(task, delivery)
        economics = task_economics(session, task, stage)
        attempts = list_attempts(session, PARENT_TASK, task.id)
        counts: dict[int, dict[str, int]] = {a.id: {} for a in attempts}
        for event in list_events(session, list(counts)):
            per = counts[event.attempt_id]
            per[event.event_type] = per.get(event.event_type, 0) + 1
        approval = delivery["approval"]["current"]
        assumptions = get_assumptions(session)
        document = {
            "format": EXPORT_FORMAT,
            "generated_at": iso_sgt(now),
            "generated_by": "Droid Forge (local export; nothing was uploaded or posted)",
            "scope": f"task {task.id} only",
            "excluded": list(EXCLUDED),
            "task": {
                "id": task.id,
                "repository": task.repository,
                "issue_number": task.issue_number,
                "issue_title": task.issue_title,
                "status": task.status,
                "stage": stage,
                "stage_label": STAGE_LABELS[stage],
                "accepted": economics["accepted"],
                "accepted_definition": economics["accepted_definition"],
                "created_at": iso_sgt(task.created_at),
                "completed_at": iso_sgt(task.completed_at),
                "error": safe_error(task.error, limit=ERROR_LIMIT) if task.error else None,
            },
            "candidate": delivery["candidate"],
            "approval": None
            if approval is None
            else {k: approval[k] for k in ("id", "candidate_sha", "approved_at", "approved_via", "grants_merge")},
            "approved_sha": approval["candidate_sha"] if approval else None,
            "attempts": [_attempt(a, counts[a.id]) for a in attempts],
            "checks": _checks(delivery["checks"]),
            "review": _review(delivery["review"]),
            "publication": _publication(delivery, now),
            "merge": delivery["merge"],
            "usage": {
                "observed_factory_credits": economics["observed_factory_credits"],
                "credit_coverage": economics["credit_coverage"],
                "credit_attempts": economics["credit_attempts"],
                "usage_coverage": economics["usage_coverage"],
                "credits_per_accepted_change": economics["credits_per_accepted_change"],
                "credits_per_accepted_detail": economics["credits_per_accepted_detail"],
                "unsuccessful_attempt_factory_credits": economics["unsuccessful_attempt_factory_credits"],
                "attempt_status_counts": economics["attempt_status_counts"],
                "candidate_review_factory_credits": economics["candidate_reviews"]["observed_factory_credits"],
                "usd": economics["usd"],
            },
            "elapsed": economics["elapsed"],
            "human_effort": economics["human_effort"],
            "assumptions": assumptions.to_dict() if assumptions else None,
            "notes": economics["notes"],
        }
    redacted: dict[str, Any] = redact(document)
    return redacted


@router.get("/api/tasks/{task_id}/evidence", response_model=None)
async def export_task_evidence(task_id: int, download: bool = False) -> JSONResponse:
    """Bounded, redacted evidence for one selected task. ``download=true`` returns it as an attachment."""
    document = task_evidence(task_id)
    if document is None:
        return JSONResponse(status_code=404, content={"detail": "task not found"})
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="forge-task-{task_id}-evidence.json"'
    return JSONResponse(content=document, headers=headers)
