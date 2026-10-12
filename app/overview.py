"""Executive overview: where each task is in the delivery path, with each stage kept distinct.

An agent turn that finished is not an accepted change, a published change is
not a CI-passing one, and nothing here is merged by Forge. "Accepted" means
the operator explicitly approved the task's current candidate commit after
its acceptance checks passed and its restricted review approved it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.delivery import delivery_state
from app.delivery_models import CIState
from app.models import ReviewTask, Task, TaskStatus
from app.review_outcomes import REVIEW_TARGET_CANDIDATE

OVERVIEW_TASK_LIMIT = 50


class Stage:
    AGENT_RUNNING = "agent_running"
    AGENT_STOPPED = "agent_stopped"
    AGENT_INTERRUPTED = "agent_interrupted"
    AGENT_FAILED = "agent_failed"
    AGENT_BLOCKED = "agent_blocked"
    NO_CANDIDATE = "no_candidate"
    AWAITING_EVIDENCE = "awaiting_evidence"
    READY_FOR_APPROVAL = "ready_for_approval"
    APPROVED = "approved"
    PUBLICATION_INCOMPLETE = "publication_incomplete"
    PUBLISHED = "published"


STAGE_LABELS = {
    Stage.AGENT_RUNNING: "Agent turn running",
    Stage.AGENT_STOPPED: "Stopped; continue explicitly if needed",
    Stage.AGENT_INTERRUPTED: "Interrupted by restart; continue explicitly",
    Stage.AGENT_FAILED: "Agent turn failed",
    Stage.AGENT_BLOCKED: "Blocked by a run limit or spending check",
    Stage.NO_CANDIDATE: "Agent finished; no usable candidate commit",
    Stage.AWAITING_EVIDENCE: "Agent finished; checks or review still needed",
    Stage.READY_FOR_APPROVAL: "Awaiting your approval",
    Stage.APPROVED: "Approved; not published",
    Stage.PUBLICATION_INCOMPLETE: "Publication incomplete",
    Stage.PUBLISHED: "Published as a draft pull request",
}

_STATUS_STAGES = {
    TaskStatus.QUEUED: Stage.AGENT_RUNNING,
    TaskStatus.RUNNING: Stage.AGENT_RUNNING,
    TaskStatus.STOPPED: Stage.AGENT_STOPPED,
    TaskStatus.INTERRUPTED: Stage.AGENT_INTERRUPTED,
    TaskStatus.FAILED: Stage.AGENT_FAILED,
    TaskStatus.BLOCKED: Stage.AGENT_BLOCKED,
}

_READINESS_STAGES = {
    "ready_for_approval": Stage.READY_FOR_APPROVAL,
    "approved": Stage.APPROVED,
    "publishing": Stage.PUBLICATION_INCOMPLETE,
    "publication_incomplete": Stage.PUBLICATION_INCOMPLETE,
    "published": Stage.PUBLISHED,
}

ACCEPTED_STAGES = (Stage.APPROVED, Stage.PUBLICATION_INCOMPLETE, Stage.PUBLISHED)
ATTENTION_STAGES = (Stage.AGENT_STOPPED, Stage.AGENT_INTERRUPTED, Stage.AGENT_FAILED, Stage.AGENT_BLOCKED)
CI_STATES = (
    CIState.PASSING,
    CIState.FAILING,
    CIState.PENDING,
    CIState.NONE_REPORTED,
    CIState.NOT_OBSERVED,
    CIState.UNAVAILABLE,
)

DEFINITIONS = {
    "agent_completed": "Tasks whose latest agent turn finished successfully. This is not acceptance.",
    "awaiting_evidence": "Agent finished with a candidate commit, but app-run checks or the restricted review "
    "have not both passed on that exact commit.",
    "awaiting_approval": "Checks passed and the restricted review approved the exact candidate commit; "
    "the operator has not approved it yet.",
    "accepted": "The operator explicitly approved the task's current candidate commit after its checks "
    "passed and its restricted review approved it. Approval is not publication, CI or merge.",
    "published": "The app pushed the approved commit to the allowed branch and verified a draft pull request "
    "whose head is that commit.",
    "remote_ci": "GitHub CI observed after publication, by state. Only published tasks have remote CI.",
    "merged": "Pull requests observed as merged on GitHub. Forge never merges or enables auto-merge.",
    "needs_attention": "Tasks stopped, interrupted, failed or blocked; none restart without an explicit action.",
    "agent_running": "Tasks with an agent turn queued or in progress right now. A running turn can be stopped "
    "from its task page.",
}


def task_stage(task: Task, delivery: dict[str, Any]) -> str:
    stage = _STATUS_STAGES.get(task.status)
    if stage is not None:
        return stage
    readiness = delivery["readiness"]["state"]
    if readiness in _READINESS_STAGES:
        return _READINESS_STAGES[readiness]
    if delivery["candidate"]["sha"] and delivery["candidate"]["status"] == "ready":
        return Stage.AWAITING_EVIDENCE
    return Stage.NO_CANDIDATE


def _task_row(task: Task, delivery: dict[str, Any], stage: str) -> dict[str, Any]:
    publication = delivery["publication"] or delivery["latest_publication"]
    pull = publication["pull_request"] if publication else None
    return {
        "id": task.id,
        "repository": task.repository,
        "issue_number": task.issue_number,
        "issue_title": task.issue_title,
        "status": task.status,
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "candidate_sha": delivery["candidate"]["sha"],
        "checks": delivery["checks"]["status"],
        "review": delivery["review"]["status"],
        "remote_ci": delivery["remote_ci"]["state"],
        "pull_request_url": pull["url"] if pull else None,
        "pull_request_number": pull["number"] if pull else None,
        "merged_observed": task.pr_state == "merged" or bool(pull and pull["merged"]),
        "approval_stale": delivery["approval"]["current"] is None
        and any(not a["applies_to_current_candidate"] for a in delivery["approval"]["history"]),
        "created_at_display": task.to_dict()["created_at_display"],
    }


def _review_counts(session: Session) -> dict[str, int]:
    reviews = session.query(ReviewTask).all()
    pr_reviews = [r for r in reviews if (r.review_target or "pull_request") != REVIEW_TARGET_CANDIDATE]
    return {
        "pr_reviews_total": len(pr_reviews),
        "pr_reviews_active": sum(1 for r in pr_reviews if r.status in TaskStatus.ACTIVE),
        "pr_reviews_completed": sum(1 for r in pr_reviews if r.status == TaskStatus.COMPLETED),
        "candidate_reviews_total": len(reviews) - len(pr_reviews),
    }


def overview_summary(session: Session) -> dict[str, Any]:
    tasks = session.query(Task).order_by(Task.created_at.desc(), Task.id.desc()).all()
    rows = []
    for task in tasks:
        delivery = delivery_state(session, task)
        rows.append(_task_row(task, delivery, task_stage(task, delivery)))
    by_stage = {stage: sum(1 for r in rows if r["stage"] == stage) for stage in STAGE_LABELS}
    published = [r for r in rows if r["stage"] == Stage.PUBLISHED]
    counts = {
        "tasks_total": len(rows),
        "agent_running": by_stage[Stage.AGENT_RUNNING],
        "agent_completed": sum(1 for t in tasks if t.status == TaskStatus.COMPLETED),
        "awaiting_evidence": by_stage[Stage.AWAITING_EVIDENCE],
        "awaiting_approval": by_stage[Stage.READY_FOR_APPROVAL],
        "accepted": sum(by_stage[s] for s in ACCEPTED_STAGES),
        "approved_unpublished": by_stage[Stage.APPROVED],
        "publication_incomplete": by_stage[Stage.PUBLICATION_INCOMPLETE],
        "published": len(published),
        "remote_ci": {state: sum(1 for r in published if r["remote_ci"] == state) for state in CI_STATES},
        "merged": sum(1 for r in rows if r["merged_observed"]),
        "needs_attention": sum(by_stage[s] for s in ATTENTION_STAGES),
        "no_candidate": by_stage[Stage.NO_CANDIDATE],
    }
    return {
        "counts": counts,
        "by_stage": by_stage,
        "reviews": _review_counts(session),
        "definitions": DEFINITIONS,
        "stage_labels": STAGE_LABELS,
        "tasks": rows[:OVERVIEW_TASK_LIMIT],
        "tasks_shown": min(len(rows), OVERVIEW_TASK_LIMIT),
        "merge": {"enabled": False, "detail": "Forge never merges or enables auto-merge"},
    }


__all__ = ["DEFINITIONS", "STAGE_LABELS", "Stage", "overview_summary", "task_stage"]
