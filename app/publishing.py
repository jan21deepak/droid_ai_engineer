"""App-owned publication primitives (same-repo pull request creation).

Nothing here runs because an agent turn completed. Finalization, follow-up,
recovery and polling only record a local candidate (``app.candidates``); these
helpers are reserved for the explicit, operator-approved publication gate that
binds checks, a restricted review and approval to the exact candidate SHA.
Every GitHub write is still refused by outward-action policy unless permitted.
"""

import logging

import httpx

from app.database import db_session
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import Task
from app.policy import OutwardAction, outward_decision
from app.repos import resolve_agent_launch_config

logger = logging.getLogger("app.publishing")


def _persist_task_pr_url(task_id: int, pr_url: str) -> None:
    """Write PR URL immediately so a later crash cannot lose the link."""
    if not pr_url:
        return
    with db_session() as session:
        db_task = session.get(Task, task_id)
        if db_task and not db_task.pull_request_url:
            db_task.pull_request_url = pr_url
            if not db_task.pr_state:
                db_task.pr_state = "open"


async def _pull_request_base(task: Task, github: GitHubClient) -> str:
    launch = resolve_agent_launch_config(repository=task.repository, repository_url=task.repository_url)
    base = launch.get("starting_ref")
    if base:
        return base
    try:
        meta = await github.get_repository(task.repository)
        return str(meta.get("default_branch") or "main")
    except Exception:
        return "main"


async def ensure_pull_request_for_task(task: Task, branch: str, github: GitHubClient) -> str | None:
    """Open (or reuse) a same-repo PR for an already published ``branch``.

    The PR is opened *within* ``task.repository`` (``head=owner:branch``,
    base = repository default branch or the configured starting ref).
    """
    if task.pull_request_url:
        return task.pull_request_url
    if not branch:
        return None
    decision = outward_decision(OutwardAction.PULL_REQUEST_CREATE, task.repository, ref=branch)
    if not decision.allowed:
        log_event(
            logger,
            logging.WARNING,
            "policy.outward_denied",
            action=OutwardAction.PULL_REQUEST_CREATE,
            repo=task.repository,
            ref=branch,
            reason=decision.reason,
            task_id=task.id,
        )
        return None

    repository = task.repository
    existing = await github.find_open_pull_request_for_head(repository, branch)
    if existing and existing.get("html_url"):
        html_url: str = existing["html_url"]
        log_event(logger, logging.INFO, "github.pr_reused", task_id=task.id, repo=repository, branch=branch)
        _persist_task_pr_url(task.id, html_url)
        return html_url

    base = await _pull_request_base(task, github)
    title = task.issue_title or f"Fix #{task.issue_number}"
    if task.issue_number and f"#{task.issue_number}" not in title:
        title = f"{title} (#{task.issue_number})"
    body_parts = [
        f"Opened by Droid Forge as a **same-repo** PR on `{repository}` for the approved candidate on `{branch}`.",
        "",
        f"Fixes #{task.issue_number}." if task.issue_number else "",
        "",
        f"Droid session: `{task.droid_session_id}`",
    ]
    body = "\n".join(body_parts).strip()

    try:
        pr = await github.create_pull_request(
            repository,
            title=title[:250],
            head=branch,
            base=base,
            body=body,
            same_repo=True,
        )
    except httpx.HTTPStatusError as exc:
        # Race: PR may have been created between find and create.
        if exc.response.status_code == 422:
            existing = await github.find_open_pull_request_for_head(repository, branch)
            if existing and existing.get("html_url"):
                reused_url: str = existing["html_url"]
                _persist_task_pr_url(task.id, reused_url)
                return reused_url
        log_event(logger, logging.WARNING, "github.pr_create_failed", task_id=task.id, repo=repository, error=str(exc))
        return None
    except Exception as exc:
        log_event(logger, logging.WARNING, "github.pr_create_failed", task_id=task.id, repo=repository, error=str(exc))
        return None

    pr_url: str | None = pr.get("html_url")
    if pr_url:
        _persist_task_pr_url(task.id, pr_url)
    return pr_url


__all__ = ["ensure_pull_request_for_task"]
