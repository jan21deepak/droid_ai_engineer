"""Explicit, operator-approved publication of one exact candidate commit.

Nothing here runs because an agent turn, review, follow-up or reconciliation
completed: only the operator's publish request reaches ``publish_candidate``.
It requires, for the task's current candidate SHA, passing app-run checks, an
approving restricted review, the operator's approval of that exact candidate
recording, and outward-action policy for the branch push and draft PR. The
workspace is re-inspected immediately before the push.

The push sends that SHA to the candidate's own (allowed) branch without force,
the remote head is then read back, and a draft PR from that branch is reused
or created and its head compared to the SHA. Every step's outcome is persisted
as it happens, so a partial failure reports what is known (pushed or not, PR
or not) instead of total success, and a retry continues without duplicating
the push or the PR. Forge never merges or enables auto-merge.
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
    candidate_refusal,
    current_approval,
    destination,
    publication_for,
    publish_blockers,
    stale_refusal,
    task_snapshot,
)
from app.delivery_git import push_candidate, recheck_candidate_workspace, remote_branch_head
from app.delivery_models import Publication, PublicationStatus
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import Task
from app.redaction import safe_error

logger = logging.getLogger("app.publishing")

# GitHub can report a reused PR's previous head for a moment after a push.
PR_HEAD_RECHECK_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

_locks: dict[int, asyncio.Lock] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _update(publication_id: int, **fields: Any) -> dict[str, Any]:
    with db_session() as session:
        pub = session.get(Publication, publication_id)
        assert pub is not None
        for key, value in fields.items():
            setattr(pub, key, value)
        pub.updated_at = _now()
        session.flush()
        return pub.to_dict()


def _result(publication: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"published": publication["status"] == PublicationStatus.PUBLISHED, "publication": publication, **extra}


async def publish_candidate(task_id: int, sha: str, *, github: GitHubClient | None = None) -> dict[str, Any]:
    """Publish exactly ``sha`` for ``task_id`` when every gate holds; see the module docstring."""
    lock = _locks.setdefault(task_id, asyncio.Lock())
    if lock.locked():
        raise DeliveryRefusedError(409, "publication_in_progress")
    async with lock:
        return await _publish(task_id, sha, github or GitHubClient())


def _gate(task_id: int, sha: str) -> tuple[Task, int, dict[str, Any] | None]:
    """Refuse unless every prerequisite holds; returns a task snapshot, the approval id and any finished result."""
    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise DeliveryRefusedError(404, "task not found")
        refusal = stale_refusal(task, sha) or candidate_refusal(task)
        if refusal is not None:
            raise refusal
        dest = destination(task)
        if not dest["allowed"]:
            raise DeliveryRefusedError(403, "policy_denied", reason=dest["reason"], destination=dest)
        blockers = publish_blockers(session, task)
        approval = current_approval(session, task)
        if blockers or approval is None:
            raise DeliveryRefusedError(409, "not_ready_to_publish", blockers=blockers)
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


async def _publish(task_id: int, sha: str, github: GitHubClient) -> dict[str, Any]:
    task, approval_id, finished = _gate(task_id, sha)
    if finished is not None:
        return finished
    recheck = await recheck_candidate_workspace(task)
    if not recheck.ok:
        raise DeliveryRefusedError(
            409, recheck.code or "stale_candidate", reason=recheck.problem, workspace_sha=recheck.head_sha
        )
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

    pushed = await _push(pub_id, task, sha, branch)
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


async def _push(pub_id: int, task: Task, sha: str, branch: str) -> dict[str, Any] | None:
    """Push (unless the remote already holds ``sha``) and verify; returns a result only when publication stops."""
    try:
        before = await remote_branch_head(task.repository, branch)
    except Exception as exc:
        return _result(
            _update(
                pub_id, status=PublicationStatus.PUSH_FAILED, pushed=False, error=f"remote branch: {safe_error(exc)}"
            )
        )
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
    state = "merged" if pr.get("merged") or pr.get("merged_at") else (pr.get("state") or "open")
    return {
        "pr_number": number if isinstance(number, int) else None,
        "pr_url": pr.get("html_url") or None,
        "pr_draft": bool(pr.get("draft")),
        "pr_state": state,
        "pr_head_sha": head.get("sha") or None,
    }


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
    github: GitHubClient, task: Task, sha: str, branch: str, base: str, title: str
) -> tuple[dict[str, Any] | None, str]:
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
    try:
        pr, action = await _find_or_create(github, task, sha, branch, base, title[:250])
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
            row.pr_state = facts["pr_state"]
    if facts["pr_head_sha"] != sha:
        return _result(
            _update(
                pub_id,
                status=PublicationStatus.PR_HEAD_MISMATCH,
                error=f"pull request head is {(facts['pr_head_sha'] or 'unknown')[:12]}, not the approved {sha[:12]}",
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
