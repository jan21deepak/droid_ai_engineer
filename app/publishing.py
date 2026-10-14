"""Explicit, operator-approved publication of one exact candidate commit.

Nothing here runs because an agent turn, review, follow-up or reconciliation
completed: only the operator's publish request reaches ``publish_candidate``.
It requires, for the task's current candidate SHA, passing app-run checks, an
approving restricted review, the operator's approval of that exact candidate
recording, and outward-action policy for the branch push and draft PR. The
task's publication reservation (``app.task_reservations``) keeps follow-up,
recovery, new evidence and approval out while publication runs, and after
the awaited GitHub and remote lookups every gate, the task's live SDK work
(a stopped turn still winding down) and the workspace's actual HEAD are
rechecked immediately before the push.

The push sends that SHA to the candidate's own (allowed) branch without force,
the remote head is then read back, and a draft PR from that branch is reused
or created and its head compared to the SHA. Publication is complete only when
that PR is open, unmerged and a draft. Every step's outcome is persisted as it
happens, so a partial failure reports what is known (pushed or not, PR or not)
instead of total success; a later attempt's failure never erases an earlier
attempt's push or PR, and a retry continues without duplicating either.
Forge never merges, converts, reopens or enables auto-merge.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.database import db_session
from app.delivery import (
    DeliveryRefusedError,
    delivery_reservation,
    publication_for,
    publication_gate,
    task_snapshot,
    workspace_refusal,
)
from app.delivery_git import push_candidate, recheck_candidate_workspace, remote_branch_head
from app.delivery_models import Publication, PublicationStatus, pull_request_state
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import Task
from app.redaction import safe_error
from app.task_reservations import TASK_BUSY, Purpose, Reservation

logger = logging.getLogger("app.publishing")

# GitHub can report a reused PR's previous head for a moment after a push.
PR_HEAD_RECHECK_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

# Once established, these facts describe an outward effect that already happened; a later
# attempt's failure or missing observation never resets them.
_KNOWN_FACTS = frozenset(
    {"pushed_at", "remote_head_sha", "remote_verified_at", "base_branch", "pr_number", "pr_url", "pr_action"}
)


def _now() -> datetime:
    return datetime.now(UTC)


def _update(publication_id: int, **fields: Any) -> dict[str, Any]:
    """Persist this attempt's step outcome without erasing what earlier attempts established.

    ``pushed`` (with its ``push_detail``) stays true once a push is known, and a
    known fact in ``_KNOWN_FACTS`` is never overwritten with ``None``. ``status``
    and ``error`` always describe the current attempt.
    """
    with db_session() as session:
        pub = session.get(Publication, publication_id)
        assert pub is not None
        already_pushed = pub.pushed is True
        for key, value in fields.items():
            if already_pushed and key in ("pushed", "push_detail") and fields.get("pushed") is not True:
                continue
            if value is None and key in _KNOWN_FACTS and getattr(pub, key) is not None:
                continue
            setattr(pub, key, value)
        pub.updated_at = _now()
        session.flush()
        return pub.to_dict()


def _result(publication: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"published": publication["status"] == PublicationStatus.PUBLISHED, "publication": publication, **extra}


async def publish_candidate(task_id: int, sha: str, *, github: GitHubClient | None = None) -> dict[str, Any]:
    """Publish exactly ``sha`` for ``task_id`` when every gate holds; see the module docstring.

    The task's publication reservation is held throughout, so follow-up or
    recovery admission, new checks, a new candidate review and approval are
    refused (``publication_in_progress``) until it ends, and a publication is
    refused (``task_busy``) while any of them, or a turn's finalization, holds
    the task.
    """
    with delivery_reservation(task_id, Purpose.PUBLICATION) as reservation:
        return await _publish(reservation, task_id, sha, github or GitHubClient())


def _gate(task_id: int, sha: str) -> tuple[Task, int, dict[str, Any] | None]:
    """Refuse unless every prerequisite holds; returns a task snapshot, the approval id and any finished result."""
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise DeliveryRefusedError(404, "task not found")
        approval = publication_gate(session, task, sha)
        existing = publication_for(session, approval)
        if existing is not None and existing.status == PublicationStatus.PUBLISHED:
            return task_snapshot(task), approval.id, _result(existing.to_dict(), already_published=True)
        if existing is not None and existing.status == PublicationStatus.PUBLISHING:
            raise DeliveryRefusedError(409, "publication_in_progress", publication_id=existing.id)
        return task_snapshot(task), approval.id, None


def _begin(task: Task, approval_id: int, branch: str) -> int:
    with db_session() as session:
        current = session.get(Task, task.id)
        if current is None or current.candidate_generation != task.candidate_generation:
            raise DeliveryRefusedError(409, "stale_candidate", reason="the candidate changed during publication")
        pub = session.query(Publication).filter(Publication.approval_id == approval_id).first()
        if pub is None:
            pub = Publication(
                task_id=task.id,
                candidate_sha=task.candidate_sha,
                approval_id=approval_id,
                repository=task.repository,
                branch=branch,
            )
            session.add(pub)
        pub.status = PublicationStatus.PUBLISHING
        pub.publish_attempts = (pub.publish_attempts or 0) + 1
        pub.error = None
        pub.updated_at = _now()
        session.flush()
        return pub.id


async def _publish(reservation: Reservation, task_id: int, sha: str, github: GitHubClient) -> dict[str, Any]:
    task, approval_id, finished = _gate(task_id, sha)
    # Even an idempotent repeat answers only for a workspace that still holds exactly the clean candidate.
    refusal = workspace_refusal(await recheck_candidate_workspace(task))
    if refusal is not None:
        raise refusal
    if finished is not None:
        return finished
    branch = task.candidate_branch or ""
    pub_id = _begin(task, approval_id, branch)
    log_event(logger, logging.INFO, "publication.started", task_id=task_id, sha=sha, branch=branch, publication=pub_id)

    base = await _base_branch(github, task.repository)
    if base is None or base == branch:
        reason = (
            "the pull request base branch could not be read from GitHub"
            if base is None
            else f"{branch!r} is the repository default branch; Forge publishes only to a separate allowed branch"
        )
        return _result(_update(pub_id, status=PublicationStatus.FAILED, pushed=False, error=reason))
    _update(pub_id, base_branch=base)

    pushed = await _push(reservation, pub_id, task, approval_id, sha, branch)
    if pushed is not None:
        return pushed
    result = await _pull_request(pub_id, task, sha, branch, base, github)
    log_event(
        logger,
        logging.INFO,
        "publication.finished",
        task_id=task_id,
        sha=sha,
        publication=pub_id,
        status=result["publication"]["status"],
    )
    return result


async def _base_branch(github: GitHubClient, repository: str) -> str | None:
    try:
        meta = await github.get_repository(repository)
    except Exception as exc:
        log_event(logger, logging.WARNING, "publication.base_lookup_failed", repo=repository, error=str(exc))
        return None
    base = meta.get("default_branch") if isinstance(meta, dict) else None
    return base if isinstance(base, str) and base else None


async def _revalidate(reservation: Reservation, task: Task, approval_id: int, sha: str) -> None:
    """Recheck, after every awaited lookup, that the approval still authorizes pushing ``sha`` now.

    The workspace is inspected first (the only await); every recorded fact is
    then read synchronously, so nothing can change between this check and the
    push that follows it.
    """
    recheck = await recheck_candidate_workspace(task)
    busy = reservation.others()
    if busy:
        raise DeliveryRefusedError(409, TASK_BUSY, reason=f"the task became busy ({', '.join(busy)})", reserved_by=busy)
    with db_session() as session:
        current = session.get(Task, task.id)
        if current is None:
            raise DeliveryRefusedError(404, "task not found")
        approval = publication_gate(session, current, sha)
        if current.candidate_generation != task.candidate_generation or approval.id != approval_id:
            raise DeliveryRefusedError(409, "stale_candidate", reason="the candidate changed during publication")
    refusal = workspace_refusal(recheck)
    if refusal is not None:
        raise refusal


async def _push(
    reservation: Reservation, pub_id: int, task: Task, approval_id: int, sha: str, branch: str
) -> dict[str, Any] | None:
    """Push (unless the remote already holds ``sha``) and verify; returns a result only when publication stops."""
    try:
        before = await remote_branch_head(task.repository, branch)
    except Exception as exc:
        return _result(
            _update(
                pub_id, status=PublicationStatus.PUSH_FAILED, pushed=False, error=f"remote branch: {safe_error(exc)}"
            )
        )
    try:
        await _revalidate(reservation, task, approval_id, sha)
    except DeliveryRefusedError as exc:
        reason = exc.extra.get("reason") or exc.detail
        _update(pub_id, status=PublicationStatus.REFUSED, error=f"refused before pushing ({exc.detail}): {reason}")
        log_event(logger, logging.WARNING, "publication.refused_at_push", publication=pub_id, code=exc.detail)
        raise
    if before == sha:
        _update(pub_id, pushed=True, push_detail="already_on_remote")
    else:
        try:
            await push_candidate(task.repository, task.id, sha, branch)
        except Exception as exc:
            return _result(
                _update(
                    pub_id,
                    status=PublicationStatus.PUSH_FAILED,
                    pushed=False,
                    push_detail="rejected",
                    remote_head_sha=before,
                    error=f"push was not accepted (never forced): {safe_error(exc)}",
                )
            )
        _update(pub_id, pushed=True, push_detail="pushed", pushed_at=_now())
    try:
        after = await remote_branch_head(task.repository, branch)
    except Exception as exc:
        return _result(
            _update(
                pub_id,
                status=PublicationStatus.REMOTE_MISMATCH,
                remote_head_sha=None,
                error=f"remote head could not be verified after the push: {safe_error(exc)}",
            )
        )
    _update(pub_id, remote_head_sha=after, remote_verified_at=_now())
    if after != sha:
        return _result(
            _update(
                pub_id,
                status=PublicationStatus.REMOTE_MISMATCH,
                error=f"remote {branch} is at {(after or 'nothing')[:12]}, not the approved {sha[:12]}",
            )
        )
    return None


def _pr_facts(pr: dict[str, Any]) -> dict[str, Any]:
    raw_head = pr.get("head")
    head: dict[str, Any] = raw_head if isinstance(raw_head, dict) else {}
    number = pr.get("number")
    return {
        "pr_number": number if isinstance(number, int) else None,
        "pr_url": pr.get("html_url") or None,
        # Only an explicit answer counts: a response that omits these proves nothing.
        "pr_draft": pr.get("draft") is True,
        "pr_state": pull_request_state(pr),
        "pr_head_sha": head.get("sha") or None,
    }


def _draft_problem(facts: dict[str, Any]) -> str | None:
    """Why the observed PR is not the open, unmerged draft publication requires, if it is not."""
    if facts["pr_state"] != "open":
        return f"is {facts['pr_state'] or 'in an unreported state'}, not open"
    if facts["pr_draft"] is not True:
        return "is not a draft"
    return None


def _pr_body(task: Task, sha: str) -> str:
    lines = [
        "Draft opened by Droid Forge after explicit operator approval of candidate "
        f"`{sha}` on `{task.candidate_branch}`.",
        "",
        "- Local acceptance checks (run by the app) passed on this exact commit.",
        "- A restricted Read/Grep/Glob Droid review approved this exact commit. That is an AI verdict, "
        "not a GitHub review.",
        "- GitHub CI on this pull request is observed separately. Forge never merges.",
    ]
    return "\n".join(lines)


async def _find_or_create(
    github: GitHubClient, task: Task, sha: str, branch: str, base: str, title: str, known_number: int | None
) -> tuple[dict[str, Any] | None, str | None]:
    """The PR to judge, and how it was obtained (``None``: the one an earlier attempt already recorded)."""
    if known_number is not None:
        # Keep one PR identity: never open a replacement for a PR this publication already found.
        return await github.get_pull_request(task.repository, known_number), None
    existing = await github.find_open_pull_request_for_head(task.repository, branch)
    if existing:
        return existing, "reused"
    try:
        pr = await github.create_pull_request(
            task.repository, title=title, head=branch, base=base, body=_pr_body(task, sha), draft=True, same_repo=True
        )
        return pr, "created"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 422:
            raise
        # An open PR for this head may have appeared since the lookup.
        return await github.find_open_pull_request_for_head(task.repository, branch), "reused"


async def _pull_request(
    pub_id: int, task: Task, sha: str, branch: str, base: str, github: GitHubClient
) -> dict[str, Any]:
    with db_session() as session:
        row = session.get(Task, task.id)
        title = (row.issue_title if row else "") or f"Droid Forge task #{task.id}"
        if row is not None and row.issue_number and f"#{row.issue_number}" not in title:
            title = f"{title} (#{row.issue_number})"
        pub = session.get(Publication, pub_id)
        known_number = pub.pr_number if pub is not None else None
    try:
        pr, action = await _find_or_create(github, task, sha, branch, base, title[:250], known_number)
    except Exception as exc:
        return _result(_update(pub_id, status=PublicationStatus.PR_FAILED, error=f"pull request: {safe_error(exc)}"))
    if not pr:
        return _result(
            _update(pub_id, status=PublicationStatus.PR_FAILED, error="no pull request could be created or found")
        )
    facts = _pr_facts(pr)
    for delay in PR_HEAD_RECHECK_DELAYS:
        if facts["pr_head_sha"] == sha or facts["pr_number"] is None:
            break
        await asyncio.sleep(delay)
        try:
            facts = _pr_facts(await github.get_pull_request(task.repository, facts["pr_number"]))
        except Exception as exc:
            log_event(logger, logging.WARNING, "publication.pr_head_recheck_failed", error=str(exc))
    _update(pub_id, pr_action=action, **facts)
    with db_session() as session:
        row = session.get(Task, task.id)
        if row is not None and facts["pr_url"]:
            row.pull_request_url = facts["pr_url"]
            row.pr_state = facts["pr_state"] or row.pr_state
    if facts["pr_head_sha"] != sha:
        return _result(
            _update(
                pub_id,
                status=PublicationStatus.PR_HEAD_MISMATCH,
                error=f"pull request head is {(facts['pr_head_sha'] or 'unknown')[:12]}, not the approved {sha[:12]}",
            )
        )
    problem = _draft_problem(facts)
    if problem is not None:
        return _result(
            _update(
                pub_id,
                status=PublicationStatus.PR_NONCOMPLIANT,
                error=f"pull request #{facts['pr_number']} {problem}; publication needs an open, unmerged draft PR "
                "at the approved commit, and Forge never converts, reopens or merges one",
            )
        )
    return _result(_update(pub_id, status=PublicationStatus.PUBLISHED, error=None))


def recover_interrupted_publications() -> int:
    """At startup, a publication left mid-flight keeps its known facts and becomes retryable."""
    with db_session() as session:
        rows = session.query(Publication).filter(Publication.status == PublicationStatus.PUBLISHING).all()
        for pub in rows:
            pub.status = PublicationStatus.INTERRUPTED
            pub.error = "the service restarted during publication; known push/PR facts are kept, retry to continue"
            pub.updated_at = _now()
        return len(rows)


__all__ = ["PR_HEAD_RECHECK_DELAYS", "publish_candidate", "recover_interrupted_publications"]
