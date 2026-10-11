"""Event-driven Droid run finalization plus GitHub state polling.

Cursor Forge polled a remote API for agent status. Droid Forge sessions run
locally and finish in background asyncio tasks, so completion is event-driven:
``launch_fix_task`` / ``launch_review_task`` start a run and hand the turn
outcome to ``finalize_fix_task`` / ``finalize_review_task``. The worker loop
(``app.reconcile``) only observes GitHub-side state.

A successful implementation, follow-up or recovery turn records a local
*candidate* (``app.candidates``); it never pushes, opens a pull request, starts
publication or grants approval. Reviews are fresh restricted (Read/Grep/Glob)
sessions over an exact base..head diff and are never resumed. Nothing merges.

Every launch, finalization, review and recovery step re-checks repository and
outward-action policy (``app.policy``) before any Git or GitHub call, so stored
work cannot bypass the current configuration.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

import httpx

from app.candidates import build_candidate_comment, record_candidate
from app.config import get_settings
from app.database import db_session
from app.droid_client import (
    DroidClient,
    DroidRunError,
    extract_branch_name,
    get_droid_client,
    map_outcome_status,
    task_workspace_dir,
)
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import ReviewTask, Task, TaskStatus
from app.policy import OutwardAction, PolicyDecision, outward_decision, repository_decision
from app.reconcile import (
    finalize_merged_running_tasks,
    parse_pr_number,
    poll_github_state,
    refresh_pr_states,
    worker_loop,
)
from app.redaction import safe_error
from app.repos import resolve_agent_launch_config
from app.review_policy import (
    REVIEW_POLICY_RESTRICTED,
    ReviewContext,
    ReviewVerdict,
    effective_verdict,
    parse_review_output,
)

__all__ = [
    "finalize_merged_running_tasks",
    "parse_pr_number",
    "poll_github_state",
    "refresh_pr_states",
    "worker_loop",
]

logger = logging.getLogger("app.worker")

REVIEW_BODY_LIMIT = 60_000
# Pseudo-action for read/work access to a repository (no GitHub write).
REPOSITORY_ACCESS = "repository_access"
REVIEW_INTERRUPTED_ERROR = (
    "review interrupted by service restart; review sessions are never resumed "
    "(reassign the pull request to start a fresh restricted review)"
)


def _policy_denied(action: str, repository: str, *, ref: str | None = None, **ctx: Any) -> PolicyDecision | None:
    """Check an outward action (or plain repository access) and log a denial."""
    if action == REPOSITORY_ACCESS:
        decision = repository_decision(repository)
    else:
        decision = outward_decision(action, repository, ref=ref)
    if decision.allowed:
        return None
    log_event(
        logger,
        logging.WARNING,
        "policy.outward_denied",
        action=action,
        repo=repository,
        ref=ref,
        reason=decision.reason,
        **ctx,
    )
    return decision


# ---------------------------------------------------------------------------
# Fix runs
# ---------------------------------------------------------------------------


async def launch_fix_task(
    task_id: int,
    *,
    repository: str,
    repository_url: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
) -> None:
    """Background launcher: prepare the workspace and start the Droid fix session."""
    denied = _policy_denied(REPOSITORY_ACCESS, repository, task_id=task_id)
    if denied is not None:
        with db_session() as session:
            db_task = session.get(Task, task_id)
            if db_task is not None:
                db_task.status = TaskStatus.FAILED
                db_task.error = f"blocked by repository policy: {denied.reason}"
        return
    droid = get_droid_client()
    launch = resolve_agent_launch_config(repository=repository, repository_url=repository_url)
    try:
        result = await droid.start_fix_run(
            repository=repository,
            repository_url=repository_url,
            task_id=task_id,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body or "",
            starting_ref=launch.get("starting_ref"),
            setup_command=launch.get("setup_command") or "",
            model=launch.get("model"),
            on_complete=partial(finalize_fix_task, task_id),
        )
    except Exception as exc:
        with db_session() as session:
            db_task = session.get(Task, task_id)
            if db_task is not None:
                db_task.status = TaskStatus.FAILED
                db_task.error = f"Droid session failed to start: {safe_error(exc)}"
        log_event(
            logger,
            logging.ERROR,
            "droid.session_start_failed",
            task_id=task_id,
            repository=repository,
            issue=issue_number,
            error=str(exc),
        )
        return

    with db_session() as session:
        db_task = session.get(Task, task_id)
        if db_task is not None:
            db_task.droid_session_id = result["session_id"]
            db_task.status = TaskStatus.RUNNING
            db_task.error = None
            if result.get("base_sha") and not db_task.base_sha:
                db_task.base_sha = result["base_sha"]
    log_event(
        logger,
        logging.INFO,
        "droid.agent_linked",
        task_id=task_id,
        session_id=result["session_id"],
        repository=repository,
        issue=issue_number,
        model=launch.get("model") or get_settings().droid_model,
        setup=result.get("setup"),
    )


def spawn_fix_launch(task_id: int, **launch_kwargs: Any) -> asyncio.Task[None]:
    """Fire the launcher in the background so webhook handlers stay fast."""
    droid = get_droid_client()
    task = asyncio.create_task(launch_fix_task(task_id, **launch_kwargs))
    droid.track(f"launch:task-{task_id}", task)
    return task


def _apply_outcome_to_task(task: Task | ReviewTask, outcome: dict[str, Any], status: str, summary: str | None) -> None:
    """Persist turn outcome fields (status, summary, runtime, tokens, cost) onto a task row."""
    settings = get_settings()
    task.status = status
    if summary:
        task.summary = summary
    if outcome.get("duration_seconds") is not None:
        task.duration_seconds = outcome["duration_seconds"]
    if outcome.get("credits") is not None:
        task.factory_credits = outcome["credits"]
    if outcome.get("tokens") is not None and isinstance(task, Task):
        # Only fix tasks track estimated_tokens; ReviewTask has no such column.
        task.estimated_tokens = outcome["tokens"]
    cost = settings.droid_usd_per_agent_run or None
    if status == TaskStatus.COMPLETED and cost is not None:
        task.cost_usd = cost
    if status == TaskStatus.FAILED:
        task.error = outcome.get("error") or (f"Droid run ended with status: {outcome.get('subtype')}")
    if task.completed_at is None:
        task.completed_at = datetime.now(UTC)


async def finalize_fix_task(task_id: int, outcome: dict[str, Any]) -> None:
    """Turn-completion handler for issue-fix / follow-up / recovery runs.

    Success records the local candidate the agent left in the workspace. It
    does not push, open a pull request or start publication: those are
    separate app-owned steps gated on checks, review and operator approval.
    """
    github = GitHubClient()
    droid = get_droid_client()
    status = map_outcome_status(outcome)
    summary = (outcome.get("text") or "").strip() or None

    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            log_event(logger, logging.WARNING, "droid.task_missing", task_id=task_id)
            return
        old_status = task.status
        _apply_outcome_to_task(task, outcome, status, summary)
        repository = task.repository
        session_id = task.droid_session_id

    log_event(
        logger,
        logging.INFO,
        "task.status_changed",
        task_id=task_id,
        session_id=session_id,
        old=old_status,
        new=status,
    )

    if status != TaskStatus.COMPLETED:
        log_event(logger, logging.WARNING, "task.failed", task_id=task_id, session_id=session_id)
        return

    if _policy_denied(REPOSITORY_ACCESS, repository, task_id=task_id):
        return
    try:
        state = await droid.inspect_workspace(repository, task_id, extract_branch_name(summary))
    except Exception as exc:
        log_event(logger, logging.WARNING, "task.candidate_inspect_failed", task_id=task_id, error=str(exc))
        return
    candidate_status = record_candidate(task_id, state)
    log_event(
        logger,
        logging.INFO,
        "task.completed",
        task_id=task_id,
        session_id=session_id,
        candidate=candidate_status,
        published=False,
    )

    with db_session() as session:
        refreshed = session.get(Task, task_id)
    if refreshed is not None and not _policy_denied(OutwardAction.ISSUE_COMMENT, repository, task_id=task_id):
        await github.post_issue_comment(repository, refreshed.issue_number, build_candidate_comment(refreshed))


# ---------------------------------------------------------------------------
# Review runs
# ---------------------------------------------------------------------------


def ensure_review_task_row(
    *,
    repository: str,
    repository_url: str = "",
    pr_number: int,
    pr_url: str = "",
    pr_title: str = "",
) -> tuple[int | None, bool]:
    """Create a ReviewTask row unless an active one already exists.

    Returns ``(review_id, created)``.
    """
    if not repository or not pr_number:
        return None, False
    with db_session() as session:
        active = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.repository == repository,
                ReviewTask.pr_number == pr_number,
                ReviewTask.status.in_(TaskStatus.ACTIVE),
            )
            .first()
        )
        if active:
            return active.id, False

        review = ReviewTask(
            repository=repository,
            repository_url=repository_url or f"https://github.com/{repository}",
            pr_number=pr_number,
            pr_title=pr_title or f"PR #{pr_number}",
            pr_url=pr_url or f"https://github.com/{repository}/pull/{pr_number}",
            status=TaskStatus.QUEUED,
        )
        session.add(review)
        session.flush()
        log_event(
            logger,
            logging.INFO,
            "droid.review_tracked",
            review_id=review.id,
            repo=repository,
            pr=pr_number,
        )
        return review.id, True


async def start_pr_review(
    *,
    repository: str,
    repository_url: str = "",
    pr_number: int,
    pr_url: str = "",
    pr_title: str = "",
) -> dict[str, Any]:
    """Ensure a review row exists and launch a fresh restricted Droid review (non-blocking)."""
    if _policy_denied(REPOSITORY_ACCESS, repository, pr=pr_number):
        return {"review_id": None, "started": False, "detail": "repository_not_allowed"}
    review_id, _created = ensure_review_task_row(
        repository=repository,
        repository_url=repository_url,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_title=pr_title,
    )
    if review_id is None:
        return {"review_id": None, "started": False, "detail": "invalid review target"}

    droid = get_droid_client()
    if not droid.configured:
        return {"review_id": review_id, "started": False, "detail": "droid_not_configured"}

    launch = asyncio.create_task(
        launch_review_task(
            review_id,
            repository=repository,
            repository_url=repository_url,
            pr_number=pr_number,
            pr_url=pr_url,
        )
    )
    try:
        droid.track(f"launch:review-{review_id}", launch)
    except DroidRunError:
        launch.cancel()
        return {"review_id": review_id, "started": False, "detail": "already_running"}
    return {"review_id": review_id, "started": True, "detail": "droid_review_started"}


def _fail_review(review_id: int, error: str) -> None:
    _mark_row_failed(ReviewTask, review_id, error, stamp_completed=True)


def _persist_review_context(review_id: int, context: ReviewContext) -> None:
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is not None:
            review.commit_sha = context.head_sha
            review.base_sha = context.base_sha
            review.diff_complete = context.complete
            review.review_policy = REVIEW_POLICY_RESTRICTED


async def _pull_request_base_ref(repository: str, pr_number: int) -> str | None:
    """The PR's own target branch, which defines the exact review base."""
    try:
        pr = await GitHubClient().get_pull_request(repository, pr_number)
    except Exception as exc:
        log_event(logger, logging.WARNING, "review.base_lookup_failed", repo=repository, pr=pr_number, error=str(exc))
        return None
    base = pr.get("base") if isinstance(pr, dict) else None
    ref = base.get("ref") if isinstance(base, dict) else None
    return ref if isinstance(ref, str) and ref.strip() else None


async def launch_review_task(
    review_id: int,
    *,
    repository: str,
    repository_url: str,
    pr_number: int,
    pr_url: str,
) -> None:
    """Background launcher: build the exact review context, then a fresh restricted session."""
    denied = _policy_denied(REPOSITORY_ACCESS, repository, review_id=review_id)
    if denied is not None:
        _fail_review(review_id, f"blocked by repository policy: {denied.reason}")
        return
    droid = get_droid_client()
    launch = resolve_agent_launch_config(repository=repository, repository_url=repository_url)
    base_ref = await _pull_request_base_ref(repository, pr_number) or launch.get("starting_ref")
    if not base_ref:
        try:
            meta = await GitHubClient().get_repository(repository)
            base_ref = meta.get("default_branch") or "main"
        except Exception:
            base_ref = "main"

    try:
        context = await droid.prepare_review_context(
            repository=repository, review_id=review_id, pr_number=pr_number, base_ref=base_ref
        )
    except Exception as exc:
        _fail_review(review_id, f"review context could not be prepared: {safe_error(exc)}")
        log_event(logger, logging.ERROR, "droid.review_context_failed", review_id=review_id, error=str(exc))
        return
    _persist_review_context(review_id, context)
    if not context.complete:
        # Never review a partial change as if it were the whole candidate.
        _fail_review(review_id, f"review not started: {context.incomplete_reason}")
        log_event(
            logger,
            logging.WARNING,
            "droid.review_diff_incomplete",
            review_id=review_id,
            reason=context.incomplete_reason,
        )
        return

    try:
        result = await droid.start_review_run(
            context=context,
            repository_url=repository_url or f"https://github.com/{repository}",
            review_id=review_id,
            pr_url=pr_url or f"https://github.com/{repository}/pull/{pr_number}",
            model=launch.get("model"),
            on_complete=partial(finalize_review_task, review_id),
        )
    except Exception as exc:
        _fail_review(review_id, f"Droid review session failed to start: {safe_error(exc)}")
        log_event(
            logger,
            logging.ERROR,
            "droid.review_start_failed",
            review_id=review_id,
            repo=repository,
            pr=pr_number,
            error=str(exc),
        )
        return

    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is not None:
            review.droid_session_id = result["session_id"]
            review.status = TaskStatus.RUNNING
            review.error = None
    log_event(
        logger,
        logging.INFO,
        "droid.review_running",
        review_id=review_id,
        session_id=result["session_id"],
        repo=repository,
        pr=pr_number,
    )


async def _post_review_verdict(github: GitHubClient, review: ReviewTask, body: str, event: str) -> tuple[bool, str]:
    """Post a PR review pinned to the reviewed commit, falling back to COMMENT when GitHub rejects the verdict.

    Returns ``(posted, event)``.
    """
    if _policy_denied(OutwardAction.PULL_REQUEST_REVIEW, review.repository, review_id=review.id):
        return False, event
    commit_id = review.commit_sha
    try:
        await github.create_pull_request_review(
            review.repository, review.pr_number, body, event=event, commit_id=commit_id
        )
        return True, event
    except httpx.HTTPStatusError as exc:
        # GitHub rejects APPROVE / REQUEST_CHANGES on the token owner's own PRs.
        if exc.response.status_code in (403, 422):
            try:
                await github.create_pull_request_review(
                    review.repository, review.pr_number, body, event="COMMENT", commit_id=commit_id
                )
                return True, "COMMENT"
            except Exception as fallback_exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "github.review_post_failed",
                    review_id=review.id,
                    error=str(fallback_exc),
                )
        else:
            log_event(
                logger,
                logging.WARNING,
                "github.review_post_failed",
                review_id=review.id,
                error=str(exc),
            )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "github.review_post_failed",
            review_id=review.id,
            error=str(exc),
        )
    return False, event


async def _pull_request_head_matches(github: GitHubClient, review: ReviewTask) -> bool:
    """True only when GitHub confirms the PR head is still the reviewed commit."""
    try:
        pr = await github.get_pull_request(review.repository, review.pr_number)
    except Exception as exc:
        log_event(logger, logging.WARNING, "review.head_check_failed", review_id=review.id, error=str(exc))
        return False
    head = (pr.get("head") or {}).get("sha") if isinstance(pr, dict) else None
    if head != review.commit_sha:
        log_event(
            logger,
            logging.WARNING,
            "review.head_moved",
            review_id=review.id,
            reviewed=review.commit_sha,
            current=head,
        )
        return False
    return True


async def finalize_review_task(review_id: int, outcome: dict[str, Any]) -> None:
    """Turn-completion handler for restricted Droid PR review sessions.

    The structured verdict is persisted with the reviewed SHA. Only a restricted
    review of a complete diff whose head still matches GitHub posts APPROVE;
    nothing here merges or enables auto-merge.
    """
    github = GitHubClient()
    status = map_outcome_status(outcome)
    summary = (outcome.get("text") or "").strip() or None

    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is None:
            log_event(logger, logging.WARNING, "droid.review_missing", review_id=review_id)
            return
        old_status = review.status
        _apply_outcome_to_task(review, outcome, status, summary)
        if status == TaskStatus.COMPLETED and summary:
            parsed = effective_verdict(
                parse_review_output(summary),
                review_policy=review.review_policy,
                diff_complete=review.diff_complete,
                reviewed_sha=review.commit_sha,
            )
            review.verdict = parsed.verdict
            review.findings = json.dumps(parsed.findings)
        refreshed = review

    log_event(
        logger,
        logging.INFO,
        "review.status_changed",
        review_id=review_id,
        old=old_status,
        new=status,
    )

    if status != TaskStatus.COMPLETED or not summary:
        log_event(
            logger,
            logging.WARNING,
            "review.failed",
            review_id=review_id,
            error=refreshed.error,
        )
        return

    verdict = refreshed.verdict
    event = {ReviewVerdict.APPROVE: "APPROVE", ReviewVerdict.REQUEST_CHANGES: "REQUEST_CHANGES"}.get(
        verdict or "", "COMMENT"
    )
    if event == "APPROVE" and not await _pull_request_head_matches(github, refreshed):
        event = "COMMENT"
    body = summary[:REVIEW_BODY_LIMIT]
    posted, event = await _post_review_verdict(github, refreshed, body, event)

    log_event(
        logger,
        logging.INFO,
        "review.completed",
        review_id=review_id,
        repo=refreshed.repository,
        pr=refreshed.pr_number,
        verdict=verdict,
        github_event=event,
        posted=posted,
        reviewed_sha=refreshed.commit_sha,
    )


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------


def _mark_row_failed[TaskOrReviewRow: Task | ReviewTask](
    model: type[TaskOrReviewRow], row_id: int, error: str, *, stamp_completed: bool = False
) -> None:
    """Mark a Task/ReviewTask row failed with the given error message."""
    with db_session() as session:
        row = session.get(model, row_id)
        if row is not None:
            row.status = TaskStatus.FAILED
            row.error = error
            if stamp_completed:
                row.completed_at = datetime.now(UTC)


def _fail_rows_outside_policy(snapshots: list[tuple[int, str | None, str]]) -> None:
    for row_id, _session_id, repository in snapshots:
        denied = _policy_denied(REPOSITORY_ACCESS, repository, row=f"task_id={row_id}")
        if denied is not None:
            _mark_row_failed(Task, row_id, f"blocked by repository policy: {denied.reason}")


async def _resume_orphaned_fix(
    task_id: int,
    session_id: str | None,
    repository: str,
    droid: DroidClient,
    finalize: Callable[[int, dict[str, Any]], Awaitable[None]],
) -> int:
    """Resume one orphaned implementation session; fail the row if the session is lost.

    Returns 1 when the row was resumed, 0 when it was marked failed.
    """
    denied = _policy_denied(REPOSITORY_ACCESS, repository, row=f"task_id={task_id}")
    if denied is not None:
        _mark_row_failed(Task, task_id, f"blocked by repository policy: {denied.reason}")
        return 0
    if not session_id:
        _mark_row_failed(Task, task_id, "dispatch interrupted by service restart")
        log_event(logger, logging.WARNING, "recovery.dispatch_lost", task_id=task_id)
        return 0
    try:
        await droid.resume_interrupted(
            session_id, cwd=task_workspace_dir(repository, task_id), on_complete=partial(finalize, task_id)
        )
    except Exception as exc:
        _mark_row_failed(Task, task_id, f"session lost after service restart: {safe_error(exc)}")
        log_event(
            logger,
            logging.WARNING,
            "recovery.session_lost",
            session_id=session_id,
            error=str(exc),
            task_id=task_id,
        )
        return 0
    with db_session() as session:
        row = session.get(Task, task_id)
        if row is not None:
            row.status = TaskStatus.RUNNING
    return 1


def _fail_orphaned_reviews(review_snapshots: list[tuple[int, str | None, str]], live: set[str]) -> None:
    """Orphaned reviews are closed, never resumed: a resumed session would not keep the tool restriction."""
    for review_id, session_id, _repository in review_snapshots:
        if session_id and session_id in live:
            continue
        error = REVIEW_INTERRUPTED_ERROR if session_id else "review dispatch interrupted by service restart"
        _fail_review(review_id, error)
        log_event(logger, logging.WARNING, "recovery.review_not_resumed", review_id=review_id)


async def recover_interrupted_tasks(droid: DroidClient | None = None) -> int:
    """Resume implementation sessions orphaned by a restart; close orphaned reviews.

    Droid sessions persist on disk, so a RUNNING fix row whose session is no
    longer streaming is resumed with a candidate-only continuation prompt. Rows
    that never got a session (dispatch interrupted) are marked failed. Review
    rows are never resumed; the operator can reassign a fresh restricted review.
    """
    droid = droid or get_droid_client()

    with db_session() as session:
        fix_rows = session.query(Task).filter(Task.status.in_(TaskStatus.ACTIVE)).all()
        fix_snapshots = [(t.id, t.droid_session_id, t.repository) for t in fix_rows]
        review_rows = session.query(ReviewTask).filter(ReviewTask.status.in_(TaskStatus.ACTIVE)).all()
        review_snapshots = [(r.id, r.droid_session_id, r.repository) for r in review_rows]

    live = droid.active_sessions
    _fail_orphaned_reviews(review_snapshots, live)

    if not droid.configured:
        # Nothing can be resumed, but stored work outside current policy is
        # still refused visibly rather than left looking active.
        _fail_rows_outside_policy(fix_snapshots)
        return 0

    recovered = 0
    for task_id, session_id, repository in fix_snapshots:
        if session_id and session_id in live:
            continue
        recovered += await _resume_orphaned_fix(task_id, session_id, repository, droid, finalize_fix_task)

    if recovered or fix_snapshots or review_snapshots:
        log_event(
            logger,
            logging.INFO,
            "recovery.completed",
            recovered=recovered,
            fixes=len(fix_snapshots),
            reviews=len(review_snapshots),
        )
    return recovered
