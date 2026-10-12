"""Read-only observation of GitHub CI for a published candidate.

Remote CI runs only after the app pushes the approved SHA, so it is observed
on the publication afterwards and never gates the first push. Pending,
failing, passing, none-reported and unavailable stay distinct: a missing
secret, a 403 or an unreachable API is ``unavailable`` with the reason, never a
pass, and opening a PR implies nothing about CI.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.database import db_session
from app.delivery_models import CIState, Publication
from app.github import GitHubClient
from app.logging_conf import log_event
from app.policy import repository_decision
from app.redaction import safe_error
from app.resilience import RetryError

logger = logging.getLogger("app.remote_ci")

MAX_CI_CHECKS = 50
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def _check_run_state(run: dict[str, Any]) -> str:
    if run.get("status") != "completed":
        return CIState.PENDING
    return CIState.PASSING if run.get("conclusion") in PASSING_CONCLUSIONS else CIState.FAILING


def _status_state(status: dict[str, Any]) -> str:
    state = status.get("state")
    if state == "success":
        return CIState.PASSING
    if state == "pending":
        return CIState.PENDING
    return CIState.FAILING


def derive_ci_state(
    check_runs: list[dict[str, Any]], statuses: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Overall state plus a bounded per-check list; any failure outranks pending, pending outranks passing."""
    items: list[dict[str, Any]] = [
        {
            "name": str(run.get("name") or "check run")[:200],
            "kind": "check_run",
            "state": _check_run_state(run),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "url": run.get("html_url") or run.get("details_url"),
        }
        for run in check_runs
    ]
    items += [
        {
            "name": str(status.get("context") or "status")[:200],
            "kind": "commit_status",
            "state": _status_state(status),
            "status": status.get("state"),
            "conclusion": None,
            "url": status.get("target_url"),
        }
        for status in statuses
    ]
    states = {item["state"] for item in items}
    if not items:
        overall = CIState.NONE_REPORTED
    elif CIState.FAILING in states:
        overall = CIState.FAILING
    elif CIState.PENDING in states:
        overall = CIState.PENDING
    else:
        overall = CIState.PASSING
    return overall, items[:MAX_CI_CHECKS]


async def _observe(pub: Publication, github: GitHubClient) -> dict[str, Any]:
    """Fields to store for one observation of ``pub``'s commit (and its PR, when known)."""
    fields: dict[str, Any] = {"ci_head_sha": pub.candidate_sha, "ci_observed_at": datetime.now(UTC)}
    decision = repository_decision(pub.repository)
    if not decision.allowed:
        return {**fields, "ci_state": CIState.UNAVAILABLE, "ci_detail": f"not observed: {decision.reason}"}
    try:
        if pub.pr_number:
            pr = await github.get_pull_request(pub.repository, pub.pr_number)
            raw_head = pr.get("head")
            head: dict[str, Any] = raw_head if isinstance(raw_head, dict) else {}
            fields["pr_head_sha"] = head.get("sha") or pub.pr_head_sha
            fields["pr_draft"] = bool(pr.get("draft"))
            fields["pr_state"] = "merged" if pr.get("merged") or pr.get("merged_at") else (pr.get("state") or "open")
        runs = await github.list_commit_check_runs(pub.repository, pub.candidate_sha)
        combined = await github.get_commit_status(pub.repository, pub.candidate_sha)
    except (httpx.HTTPStatusError, RetryError) as exc:
        error = exc.last_error if isinstance(exc, RetryError) else exc
        code = getattr(getattr(error, "response", None), "status_code", None)
        reason = f"GitHub returned HTTP {code}" if code else safe_error(exc)
        return {**fields, "ci_state": CIState.UNAVAILABLE, "ci_detail": f"{reason}; CI state is unknown, not passing"}
    except (httpx.HTTPError, OSError) as exc:
        return {
            **fields,
            "ci_state": CIState.UNAVAILABLE,
            "ci_detail": f"GitHub could not be reached: {safe_error(exc)}",
        }
    statuses = [s for s in (combined.get("statuses") or []) if isinstance(s, dict)]
    state, items = derive_ci_state(runs, statuses)
    detail: str | None = None
    if state == CIState.NONE_REPORTED:
        detail = "GitHub reports no checks for this commit yet"
    elif fields.get("pr_head_sha") and fields["pr_head_sha"] != pub.candidate_sha:
        detail = "the pull request head has moved past the published commit"
    return {**fields, "ci_state": state, "ci_detail": detail, "ci_checks": json.dumps(items)}


async def observe_publication_ci(publication_id: int, github: GitHubClient | None = None) -> dict[str, Any] | None:
    github = github or GitHubClient()
    with db_session() as session:
        pub = session.get(Publication, publication_id)
        if pub is None:
            return None
        session.expunge(pub)
    if not pub.pushed:
        return pub.ci_dict()
    fields = await _observe(pub, github)
    with db_session() as session:
        row = session.get(Publication, publication_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        session.flush()
        result = row.ci_dict()
    log_event(logger, logging.INFO, "ci.observed", publication=publication_id, state=result["state"])
    return result


def latest_pushed_publication_id(task_id: int) -> int | None:
    with db_session() as session:
        pub = (
            session.query(Publication)
            .filter(Publication.task_id == task_id, Publication.pushed.is_(True))
            .order_by(Publication.id.desc())
            .first()
        )
        return pub.id if pub else None


async def refresh_published_ci(github: GitHubClient) -> int:
    """Poller pass: observe the latest pushed publication of each task whose PR is not closed or merged."""
    with db_session() as session:
        rows = (
            session.query(Publication)
            .filter(Publication.pushed.is_(True))
            .order_by(Publication.task_id, Publication.id.desc())
            .all()
        )
        seen: set[int] = set()
        latest: list[int] = []
        for pub in rows:
            if pub.task_id in seen:
                continue
            seen.add(pub.task_id)
            if pub.pr_state not in ("closed", "merged"):
                latest.append(pub.id)
    for publication_id in latest:
        try:
            await observe_publication_ci(publication_id, github)
        except Exception as exc:
            log_event(logger, logging.WARNING, "ci.observe_failed", publication=publication_id, error=str(exc))
    return len(latest)


__all__ = ["derive_ci_state", "latest_pushed_publication_id", "observe_publication_ci", "refresh_published_ci"]
