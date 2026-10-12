"""Commit-bound delivery state: candidate, checks, review, approval, publication and remote CI.

Each fact names the SHA it is about and only counts for the task's *current*
candidate. Readiness is derived here for both the UI and backend enforcement:

- Ready for approval: the candidate is ready (committed, clean), every
  configured acceptance check passed on that exact SHA, and the latest
  restricted candidate review of that SHA approves it.
- Approved: the operator explicitly approved that SHA for that candidate
  recording. Approval is publication approval only, never merge permission.
- Published: the app pushed the SHA, verified the remote head and the draft
  PR head. Remote CI is observed afterwards and is never a prerequisite.

Agent completion alone is none of these.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.candidates import CandidateStatus
from app.check_config import configured_checks
from app.database import db_session
from app.delivery_git import recheck_candidate_workspace
from app.delivery_models import (
    CheckResult,
    CheckRun,
    CheckStatus,
    CIState,
    DeliveryApproval,
    Publication,
    PublicationStatus,
)
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.policy import OutwardAction, configured_publication_branch, outward_decision, repository_decision
from app.review_outcomes import REVIEW_TARGET_CANDIDATE
from app.review_policy import REVIEW_POLICY_RESTRICTED, ReviewVerdict

logger = logging.getLogger("app.delivery")

HISTORY_LIMIT = 10


class DeliveryRefusedError(Exception):
    """A delivery request refused before any outward effect."""

    def __init__(self, status_code: int, detail: str, **extra: Any):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.extra = extra


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def candidate_refusal(task: Task) -> DeliveryRefusedError | None:
    """Preconditions shared by checks, candidate review, approval and publication."""
    decision = repository_decision(task.repository)
    if not decision.allowed:
        return DeliveryRefusedError(403, "repository_not_allowed", reason=decision.reason)
    if task.status in TaskStatus.ACTIVE:
        return DeliveryRefusedError(409, "task_active", reason="an agent turn is still active on this task")
    if not task.candidate_sha or task.candidate_status != CandidateStatus.READY:
        return DeliveryRefusedError(
            409,
            "candidate_not_ready",
            reason=f"candidate status is {task.candidate_status or 'unknown'}; a committed, clean candidate is required",
            candidate_status=task.candidate_status,
        )
    return None


# ---- checks -----------------------------------------------------------------


def latest_check_run(session: Session, task_id: int, sha: str | None) -> CheckRun | None:
    if not sha:
        return None
    return (
        session.query(CheckRun)
        .filter(CheckRun.task_id == task_id, CheckRun.candidate_sha == sha)
        .order_by(CheckRun.id.desc())
        .first()
    )


def check_results(session: Session, run_id: int) -> list[CheckResult]:
    return session.query(CheckResult).filter(CheckResult.run_id == run_id).order_by(CheckResult.position).all()


def _check_status(run: CheckRun | None, results: list[CheckResult], configured: list[str]) -> str:
    if run is None:
        return "missing"
    if run.status in CheckStatus.ACTIVE:
        return "running"
    if run.status == CheckStatus.FAILED:
        return "failed"
    if run.status != CheckStatus.PASSED or run.evaluated_sha != run.candidate_sha:
        return "incomplete"
    passed = {r.name for r in results if r.status == CheckStatus.PASSED}
    if not set(configured) <= passed or any(r.required and r.status != CheckStatus.PASSED for r in results):
        # A check added to the configuration since this run has not been run on this SHA.
        return "outdated_configuration"
    return "passed"


def checks_summary(session: Session, task: Task) -> dict[str, Any]:
    config = configured_checks()
    run = latest_check_run(session, task.id, task.candidate_sha)
    results = check_results(session, run.id) if run else []
    if config.error:
        status = "configuration_invalid"
    elif not config.checks:
        status = "not_configured"
    else:
        status = _check_status(run, results, config.names)
    history = session.query(CheckRun).filter(CheckRun.task_id == task.id).order_by(CheckRun.id.desc())
    return {
        "status": status,
        "passed": status == "passed",
        "candidate_sha": task.candidate_sha,
        "configuration": config.to_dict(),
        "latest_run": run.to_dict(results) if run else None,
        "history": [
            {
                "id": r.id,
                "candidate_sha": r.candidate_sha,
                "evaluated_sha": r.evaluated_sha,
                "status": r.status,
                "current": r.candidate_sha == task.candidate_sha,
            }
            for r in history.limit(HISTORY_LIMIT).all()
        ],
        "executed_by": "app",
    }


_CHECK_BLOCKERS = {
    "configuration_invalid": "the acceptance check configuration is invalid",
    "not_configured": "no acceptance checks are configured (FORGE_ACCEPTANCE_CHECKS)",
    "missing": "acceptance checks have not run on this candidate",
    "running": "acceptance checks are still running",
    "failed": "a required acceptance check failed on this candidate",
    "incomplete": "the latest acceptance check run did not complete",
    "outdated_configuration": "the configured checks changed since the last run; run them again",
}


# ---- candidate review ----------------------------------------------------------


def candidate_reviews(session: Session, task_id: int) -> list[ReviewTask]:
    return (
        session.query(ReviewTask)
        .filter(ReviewTask.task_id == task_id, ReviewTask.review_target == REVIEW_TARGET_CANDIDATE)
        .order_by(ReviewTask.id.desc())
        .all()
    )


def _review_status(review: ReviewTask | None, task: Task) -> str:
    if review is None:
        return "missing"
    if review.status != TaskStatus.COMPLETED:
        return "incomplete"
    if (
        review.review_policy != REVIEW_POLICY_RESTRICTED
        or review.diff_complete is not True
        or review.base_sha != task.base_sha
    ):
        return "incomplete"
    if review.verdict == ReviewVerdict.APPROVE:
        return "approving"
    if review.verdict == ReviewVerdict.REQUEST_CHANGES:
        return "changes_requested"
    return "inconclusive"


def review_summary(session: Session, task: Task) -> dict[str, Any]:
    reviews = candidate_reviews(session, task.id)
    active = next((r for r in reviews if r.status in TaskStatus.ACTIVE), None)
    latest = next(
        (
            r
            for r in reviews
            if r.status not in TaskStatus.ACTIVE and r.commit_sha and r.commit_sha == task.candidate_sha
        ),
        None,
    )
    status = "pending" if active is not None else _review_status(latest, task)
    return {
        "status": status,
        "approving": status == "approving",
        "candidate_sha": task.candidate_sha,
        "active_review_id": active.id if active else None,
        "latest": latest.to_dict() if latest else None,
        "history": [
            {"id": r.id, "reviewed_sha": r.commit_sha, "status": r.status, "ai_verdict": r.verdict}
            for r in reviews[:HISTORY_LIMIT]
        ],
        "tools": "Read, Grep, Glob only; the reviewer runs no commands or tests",
    }


_REVIEW_BLOCKERS = {
    "missing": "no restricted review of this candidate has completed",
    "pending": "the restricted review is still running",
    "incomplete": "the latest review of this candidate did not complete over the exact diff",
    "changes_requested": "the restricted review requested changes",
    "inconclusive": "the restricted review did not reach an approving verdict",
}


# ---- approval and publication ---------------------------------------------------


def current_approval(session: Session, task: Task) -> DeliveryApproval | None:
    if not task.candidate_sha or task.candidate_status != CandidateStatus.READY:
        return None
    return (
        session.query(DeliveryApproval)
        .filter(
            DeliveryApproval.task_id == task.id,
            DeliveryApproval.candidate_sha == task.candidate_sha,
            DeliveryApproval.candidate_generation == (task.candidate_generation or 0),
        )
        .order_by(DeliveryApproval.id.desc())
        .first()
    )


def approval_summary(session: Session, task: Task) -> dict[str, Any]:
    current = current_approval(session, task)
    history = (
        session.query(DeliveryApproval)
        .filter(DeliveryApproval.task_id == task.id)
        .order_by(DeliveryApproval.id.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    return {
        "status": "approved" if current else "not_approved",
        "current": current.to_dict(current=True) if current else None,
        "history": [a.to_dict(current=current is not None and a.id == current.id) for a in history],
        "meaning": "publication approval of this exact commit only; not a GitHub review and no merge permission",
    }


def destination(task: Task) -> dict[str, Any]:
    branch = task.candidate_branch
    push = outward_decision(OutwardAction.BRANCH_PUSH, task.repository, ref=branch or "")
    pull = outward_decision(OutwardAction.PULL_REQUEST_CREATE, task.repository, ref=branch or "")
    denied = next((d for d in (push, pull) if not d.allowed), None)
    reason = denied.reason if denied else None
    if reason is None and configured_publication_branch(branch) is None:
        # local_dev otherwise allows any ref; publication always needs an explicitly listed branch.
        reason = f"branch {branch!r} is not listed in FORGE_ALLOWED_BRANCHES"
    return {
        "repository": task.repository,
        "branch": branch,
        "pull_request": "draft",
        "base_branch": "repository default branch, read from GitHub at publication",
        "allowed": reason is None,
        "reason": reason,
        "force_push": False,
        "merge": "never",
    }


def latest_publication(session: Session, task_id: int) -> Publication | None:
    return session.query(Publication).filter(Publication.task_id == task_id).order_by(Publication.id.desc()).first()


def publication_for(session: Session, approval: DeliveryApproval | None) -> Publication | None:
    if approval is None:
        return None
    return session.query(Publication).filter(Publication.approval_id == approval.id).first()


NOT_PUBLISHED_CI: dict[str, Any] = {
    "state": CIState.NOT_PUBLISHED,
    "detail": "GitHub CI can only run after the candidate is pushed; it is not a publication prerequisite",
    "observed_at": None,
    "observed_sha": None,
    "stale": False,
    "checks": [],
    "source": "github",
    "prerequisite_for_publication": False,
}


def approval_blockers(session: Session, task: Task) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    if task.status in TaskStatus.ACTIVE:
        blockers.append(_blocker("task_active", "an agent turn is still active on this task"))
    if not task.candidate_sha or task.candidate_status != CandidateStatus.READY:
        blockers.append(
            _blocker(
                "candidate_not_ready",
                f"candidate status is {task.candidate_status or 'unknown'}; a committed, clean candidate is required",
            )
        )
        return blockers
    checks = checks_summary(session, task)["status"]
    if checks != "passed":
        blockers.append(_blocker(f"checks_{checks}", _CHECK_BLOCKERS[checks]))
    review = review_summary(session, task)["status"]
    if review != "approving":
        blockers.append(_blocker(f"review_{review}", _REVIEW_BLOCKERS[review]))
    return blockers


def publish_blockers(session: Session, task: Task) -> list[dict[str, str]]:
    blockers = approval_blockers(session, task)
    if current_approval(session, task) is None:
        blockers.append(_blocker("approval_missing", "the operator has not approved this exact candidate"))
    dest = destination(task)
    if not dest["allowed"]:
        blockers.append(_blocker("policy_denied", dest["reason"] or "publication is not permitted"))
    return blockers


def _readiness_state(approval_ready: bool, approval: DeliveryApproval | None, publication: Publication | None) -> str:
    if publication is not None and publication.status == PublicationStatus.PUBLISHED:
        return "published"
    if publication is not None and publication.status == PublicationStatus.PUBLISHING:
        return "publishing"
    if publication is not None:
        return "publication_incomplete"
    if approval is not None and approval_ready:
        return "approved"
    if approval_ready:
        return "ready_for_approval"
    return "not_ready"


def delivery_state(session: Session, task: Task) -> dict[str, Any]:
    """Everything the operator needs to judge (and the backend enforces for) this task's candidate."""
    approval = current_approval(session, task)
    current_pub = publication_for(session, approval)
    latest_pub = latest_publication(session, task.id)
    to_approve = approval_blockers(session, task)
    to_publish = publish_blockers(session, task)
    if current_pub is not None and current_pub.status == PublicationStatus.PUBLISHED:
        to_publish = []
    ci_source = current_pub or latest_pub
    remote_ci = ci_source.ci_dict() if ci_source is not None and ci_source.pushed else dict(NOT_PUBLISHED_CI)
    return {
        "task_id": task.id,
        "candidate": {
            "sha": task.candidate_sha,
            "branch": task.candidate_branch,
            "status": task.candidate_status,
            "generation": task.candidate_generation,
            "base_sha": task.base_sha,
        },
        "checks": checks_summary(session, task),
        "review": review_summary(session, task),
        "approval": approval_summary(session, task),
        "destination": destination(task),
        "publication": current_pub.to_dict() if current_pub else None,
        "latest_publication": latest_pub.to_dict() if latest_pub else None,
        "remote_ci": remote_ci,
        "readiness": {
            "state": _readiness_state(not to_approve, approval, current_pub),
            "ready_for_approval": not to_approve and approval is None,
            "approval_blockers": to_approve,
            "ready_to_publish": not to_publish and (current_pub is None or current_pub.status != "published"),
            "publish_blockers": to_publish,
        },
        "merge": {"enabled": False, "detail": "Forge never merges or enables auto-merge"},
    }


def task_delivery(task_id: int) -> dict[str, Any] | None:
    with db_session() as session:
        task = session.get(Task, task_id)
        return delivery_state(session, task) if task is not None else None


def stale_refusal(task: Task, sha: str) -> DeliveryRefusedError | None:
    if sha != task.candidate_sha:
        return DeliveryRefusedError(
            409,
            "stale_candidate",
            reason="the requested commit is not the task's current candidate",
            requested_sha=sha,
            current_sha=task.candidate_sha,
        )
    return None


def task_snapshot(task: Task) -> Task:
    """A detached copy of the fields Git rechecks need, safe to use after the DB session closes."""
    return Task(
        id=task.id,
        repository=task.repository,
        candidate_sha=task.candidate_sha,
        candidate_branch=task.candidate_branch,
        candidate_status=task.candidate_status,
        candidate_generation=task.candidate_generation,
    )


async def approve_candidate(task_id: int, sha: str, *, approved_via: str) -> dict[str, Any]:
    """Record the operator's approval of exactly ``sha``, after rechecking evidence and the workspace."""
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise DeliveryRefusedError(404, "task not found")
        refusal = stale_refusal(task, sha) or candidate_refusal(task)
        if refusal is not None:
            raise refusal
        blockers = approval_blockers(session, task)
        if blockers:
            raise DeliveryRefusedError(409, "not_ready_for_approval", blockers=blockers)
        existing = current_approval(session, task)
        if existing is not None:
            return {"approval": existing.to_dict(current=True), "already_approved": True}
        snapshot = task_snapshot(task)

    recheck = await recheck_candidate_workspace(snapshot)
    if not recheck.ok:
        raise DeliveryRefusedError(
            409, recheck.code or "stale_candidate", reason=recheck.problem, workspace_sha=recheck.head_sha
        )

    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise DeliveryRefusedError(404, "task not found")
        if task.candidate_sha != sha or task.candidate_generation != snapshot.candidate_generation:
            raise DeliveryRefusedError(409, "stale_candidate", reason="the candidate changed during approval")
        blockers = approval_blockers(session, task)
        if blockers:
            raise DeliveryRefusedError(409, "not_ready_for_approval", blockers=blockers)
        run = latest_check_run(session, task.id, sha)
        review = review_summary(session, task)["latest"]
        approval = DeliveryApproval(
            task_id=task.id,
            candidate_sha=sha,
            candidate_generation=task.candidate_generation or 0,
            approved_via=approved_via,
            check_run_id=run.id if run else None,
            review_id=review["id"] if review else None,
        )
        session.add(approval)
        session.flush()
        result = approval.to_dict(current=True)
    log_event(logger, logging.INFO, "delivery.approved", task_id=task_id, sha=sha, approval_id=result["id"])
    return {"approval": result, "already_approved": False}


__all__ = [
    "DeliveryRefusedError",
    "approval_blockers",
    "approve_candidate",
    "candidate_refusal",
    "candidate_reviews",
    "current_approval",
    "delivery_state",
    "destination",
    "latest_check_run",
    "publication_for",
    "publish_blockers",
    "stale_refusal",
    "task_delivery",
    "task_snapshot",
]
