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

Every turn is a ``RunAttempt`` (``app.attempts``) committed before the SDK
turn is spawned; completion callbacks are bound to their attempt. Restart
recovery and explicit continuations live in ``app.recovery``.

Every launch, finalization, review and recovery step re-checks repository and
outward-action policy (``app.policy``) before any Git or GitHub call, so stored
work cannot bypass the current configuration.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.attempts import (
    AttemptConflictError,
    check_admission,
    claim_active_attempt,
    fail_attempt_start,
    finish_attempt,
    mark_attempt_running,
    record_attempt_candidate,
    retain_late_usage,
    rollup_parent_usage,
    set_attempt_running,
    start_attempt,
)
from app.candidates import build_candidate_comment, record_candidate
from app.config import get_settings
from app.database import db_session
from app.droid_client import (
    DroidRunError,
    extract_branch_name,
    get_droid_client,
    map_outcome_status,
    task_launch_key,
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
from app.redaction import redact_text, safe_error
from app.repos import resolve_agent_launch_config
from app.review_outcomes import (
    REVIEW_TARGET_CANDIDATE,
    REVIEW_TARGET_PULL_REQUEST,
    GitHubReviewOutcome,
    classify_github_outcome,
    interrupt_pending,
    mark_unposted,
    set_github_outcome,
)
from app.review_policy import (
    REVIEW_POLICY_RESTRICTED,
    ReviewBinding,
    ReviewContext,
    ReviewVerdict,
    effective_verdict,
    parse_review_output,
    review_base_ref,
)
from app.run_events import TurnRecorder
from app.run_limits import RunRefusedError
from app.run_models import PARENT_REVIEW, PARENT_TASK, AttemptKind, RunAttempt
from app.task_reservations import Purpose, claim

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


def policy_denied(action: str, repository: str, *, ref: str | None = None, **ctx: Any) -> PolicyDecision | None:
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
    denied = policy_denied(REPOSITORY_ACCESS, repository, task_id=task_id)
    if denied is not None:
        _mark_row_failed(Task, task_id, f"blocked by repository policy: {denied.reason}")
        return
    droid = get_droid_client()
    launch = resolve_agent_launch_config(repository=repository, repository_url=repository_url)
    attempt_id = _start_fix_attempt(task_id, launch.get("model") or getattr(droid, "model", None))
    if attempt_id is None:
        return
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
            on_complete=partial(finalize_fix_task, task_id, attempt_id=attempt_id),
            observer=TurnRecorder(attempt_id),
        )
    except Exception as exc:
        error = f"Droid session failed to start: {safe_error(exc)}"
        # A task stopped while it was starting keeps its stopped outcome.
        if fail_attempt_start(attempt_id, error):
            _mark_row_failed(Task, task_id, error)
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

    # Conditional: a turn that already finished must not be turned back into "running".
    still_active = mark_attempt_running(attempt_id, session_id=result["session_id"], base_sha=result.get("base_sha"))
    with db_session() as session:
        db_task = session.get(Task, task_id)
        if db_task is not None:
            db_task.droid_session_id = result["session_id"]
            if still_active and db_task.status in TaskStatus.ACTIVE:
                db_task.status = TaskStatus.RUNNING
                db_task.error = None
            if result.get("base_sha") and not db_task.base_sha:
                db_task.base_sha = result["base_sha"]
    log_event(
        logger,
        logging.INFO,
        "droid.agent_linked",
        task_id=task_id,
        attempt_id=attempt_id,
        session_id=result["session_id"],
        repository=repository,
        issue=issue_number,
        model=launch.get("model") or get_settings().droid_model,
        setup=result.get("setup"),
    )


def _start_fix_attempt(task_id: int, requested_model: str | None) -> int | None:
    """Commit the implementation attempt for a queued task; ``None`` if it must not start."""
    with db_session() as session:
        task = session.get(Task, task_id)
        queued = task is not None and task.status == TaskStatus.QUEUED
    if not queued:
        log_event(logger, logging.WARNING, "attempt.start_rejected", task_id=task_id, reason="task is not queued")
        return None
    try:
        return start_attempt(
            PARENT_TASK,
            task_id,
            AttemptKind.FIX,
            requested_model=requested_model or get_settings().droid_model,
            parent_status=TaskStatus.RUNNING,
        ).attempt_id
    except (AttemptConflictError, LookupError, RunRefusedError) as exc:
        log_event(logger, logging.WARNING, "attempt.start_rejected", task_id=task_id, reason=str(exc))
        return None


def spawn_fix_launch(task_id: int, **launch_kwargs: Any) -> asyncio.Task[None]:
    """Fire the launcher in the background so webhook handlers stay fast."""
    droid = get_droid_client()
    task = asyncio.create_task(launch_fix_task(task_id, **launch_kwargs))
    droid.track(task_launch_key(task_id), task)
    return task


def _apply_outcome_to_task(
    session: Session,
    task: Task | ReviewTask,
    attempt: RunAttempt,
    outcome: dict[str, Any],
    status: str,
    summary: str | None,
) -> None:
    """Finish ``attempt`` and project its outcome onto the parent row.

    Usage lives on the attempt; a tracked parent's usage columns are the sum of
    its attempts, and a pre-attempt parent's usage columns are left as history.
    """
    settings = get_settings()
    finish_attempt(attempt, status=status, outcome=outcome, summary=summary)
    task.status = status
    if summary:
        task.summary = summary
    if outcome.get("duration_seconds") is not None:
        task.duration_seconds = outcome["duration_seconds"]
    rollup_parent_usage(session, attempt.parent_type, task)
    cost = settings.droid_usd_per_agent_run or None
    if status == TaskStatus.COMPLETED and cost is not None and task.attempts_tracked:
        task.cost_usd = cost
    if status == TaskStatus.FAILED:
        task.error = outcome.get("error") or (f"Droid run ended with status: {outcome.get('subtype')}")
    if task.completed_at is None:
        task.completed_at = datetime.now(UTC)


async def finalize_fix_task(task_id: int, outcome: dict[str, Any], *, attempt_id: int) -> None:
    """Turn-completion handler for issue-fix / follow-up / recovery runs.

    Only the still-active attempt ``attempt_id`` is finalized; a duplicate or
    stale callback changes nothing, counts no usage and makes no call.

    Success records the local candidate the agent left in the workspace. It
    does not push, open a pull request or start publication: those are
    separate app-owned steps gated on checks, review and operator approval.

    From the terminal status until the candidate is recorded the task holds a
    finalization reservation: the task already looks finished but still shows
    the previous candidate, so publication must not start in between.
    """
    github = GitHubClient()
    with claim(task_id, Purpose.FINALIZATION):
        repository = await _finalize_fix_turn(task_id, outcome, attempt_id)
    if repository is None:
        return
    with db_session() as session:
        refreshed = session.get(Task, task_id)
    if refreshed is not None and not policy_denied(OutwardAction.ISSUE_COMMENT, repository, task_id=task_id):
        await github.post_issue_comment(repository, refreshed.issue_number, build_candidate_comment(refreshed))


async def _finalize_fix_turn(task_id: int, outcome: dict[str, Any], attempt_id: int) -> str | None:
    """Apply the turn's outcome and record its candidate; returns the repository when a candidate was recorded."""
    droid = get_droid_client()
    status = map_outcome_status(outcome)
    summary = (outcome.get("text") or "").strip() or None

    with db_session() as session:
        task = session.get(Task, task_id)
        if task is None:
            log_event(logger, logging.WARNING, "droid.task_missing", task_id=task_id)
            return None
        attempt = claim_active_attempt(session, attempt_id, PARENT_TASK, task_id)
        if attempt is not None:
            old_status = task.status
            _apply_outcome_to_task(session, task, attempt, outcome, status, summary)
            repository = task.repository
            session_id = task.droid_session_id
    if attempt is None:
        log_event(
            logger,
            logging.WARNING,
            "attempt.callback_rejected",
            task_id=task_id,
            attempt_id=attempt_id,
            reason="attempt is not active for this task",
        )
        # A stopped attempt keeps its outcome; only usage it reports late is retained, once.
        retain_late_usage(attempt_id, outcome)
        return None

    log_event(
        logger,
        logging.INFO,
        "task.status_changed",
        task_id=task_id,
        attempt_id=attempt_id,
        session_id=session_id,
        old=old_status,
        new=status,
    )

    if status != TaskStatus.COMPLETED:
        log_event(logger, logging.WARNING, "task.failed", task_id=task_id, session_id=session_id)
        return None

    if policy_denied(REPOSITORY_ACCESS, repository, task_id=task_id):
        return None
    try:
        state = await droid.inspect_workspace(repository, task_id, extract_branch_name(summary))
    except Exception as exc:
        log_event(logger, logging.WARNING, "task.candidate_inspect_failed", task_id=task_id, error=str(exc))
        return None
    candidate_status = record_candidate(task_id, state)
    record_attempt_candidate(attempt_id, state.head_sha)
    log_event(
        logger,
        logging.INFO,
        "task.completed",
        task_id=task_id,
        session_id=session_id,
        candidate=candidate_status,
        published=False,
    )
    return repository


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
            review_target=REVIEW_TARGET_PULL_REQUEST,
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
    """Create a review row and launch a fresh restricted Droid review over it (non-blocking).

    Only a newly created row is launched. An active row already belongs to a
    review session (or one being prepared), so reassignment is refused rather
    than replacing that row's context; a terminal row gets a fresh row.
    """
    if policy_denied(REPOSITORY_ACCESS, repository, pr=pr_number):
        return {"review_id": None, "started": False, "detail": "repository_not_allowed"}
    review_id, created = ensure_review_task_row(
        repository=repository,
        repository_url=repository_url,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_title=pr_title,
    )
    if review_id is None:
        return {"review_id": None, "started": False, "detail": "invalid review target"}
    if not created:
        log_event(logger, logging.INFO, "review.reassign_rejected", review_id=review_id, repo=repository, pr=pr_number)
        return {"review_id": review_id, "started": False, "detail": "review_already_active"}

    droid = get_droid_client()
    if not droid.configured:
        # Closed rather than left queued, so a later assignment can start fresh.
        _fail_review(review_id, "review not started: Droid is not configured")
        return {"review_id": review_id, "started": False, "detail": "droid_not_configured"}
    refused = _review_refusal(review_id)
    if refused is not None:
        return {
            "review_id": review_id,
            "started": False,
            "detail": refused.code,
            "reason": refused.detail,
            "blocked_attempt_id": refused.blocked_attempt_id,
        }

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
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is not None and review.status in TaskStatus.ACTIVE:
            review.status = TaskStatus.FAILED
            review.error = error
            review.completed_at = datetime.now(UTC)
            mark_unposted(review)


def _review_refusal(review_id: int) -> RunRefusedError | None:
    """Run limits and spending checks for a review before any GitHub or workspace work."""
    try:
        check_admission(PARENT_REVIEW, review_id, AttemptKind.REVIEW)
    except RunRefusedError as exc:
        return exc
    except LookupError:
        return None
    return None


def _awaiting_launch(review: ReviewTask | None) -> bool:
    """A queued row that no session or review context has been attached to yet."""
    return (
        review is not None
        and review.status == TaskStatus.QUEUED
        and review.droid_session_id is None
        and review.commit_sha is None
    )


def _review_awaits_launch(review_id: int) -> bool:
    with db_session() as session:
        return _awaiting_launch(session.get(ReviewTask, review_id))


def _persist_review_context(review_id: int, context: ReviewContext) -> bool:
    """Attach the review context to a still-unbound row; ``False`` if the row was already taken."""
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is None or not _awaiting_launch(review):
            return False
        review.commit_sha = context.head_sha
        review.base_sha = context.base_sha
        review.diff_complete = context.complete
        review.review_policy = REVIEW_POLICY_RESTRICTED
        return True


def _bound_to(review: ReviewTask, binding: ReviewBinding) -> bool:
    return binding.matches(
        base_sha=review.base_sha,
        head_sha=review.commit_sha,
        diff_complete=review.diff_complete,
        review_policy=review.review_policy,
    )


def _bind_review_session(review_id: int, binding: ReviewBinding, session_id: str, *, attempt_id: int) -> None:
    """Record the session reviewing ``binding`` on its row and attempt, unless the row moved on."""
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is None or review.status not in TaskStatus.ACTIVE:
            return
        attempt = claim_active_attempt(session, attempt_id, PARENT_REVIEW, review_id)
        if (
            attempt is None
            or review.droid_session_id not in (None, session_id)
            or attempt.session_id not in (None, session_id)
            or not _bound_to(review, binding)
        ):
            log_event(
                logger, logging.WARNING, "review.session_bind_rejected", review_id=review_id, session_id=session_id
            )
            return
        set_attempt_running(attempt, session_id=session_id)
        review.droid_session_id = session_id
        review.status = TaskStatus.RUNNING
        review.error = None


async def _pull_request_base_ref(repository: str, pr_number: int) -> str | None:
    """The PR's own target branch, which defines the exact review base; ``None`` if it is not known."""
    try:
        pr = await GitHubClient().get_pull_request(repository, pr_number)
    except Exception as exc:
        log_event(logger, logging.WARNING, "review.base_lookup_failed", repo=repository, pr=pr_number, error=str(exc))
        return None
    base = pr.get("base") if isinstance(pr, dict) else None
    return review_base_ref(base.get("ref") if isinstance(base, dict) else None)


async def launch_review_task(
    review_id: int,
    *,
    repository: str,
    repository_url: str,
    pr_number: int,
    pr_url: str,
) -> None:
    """Background launcher: build the exact review context, then a fresh restricted session.

    Only a queued row with no session or context is launched. The base is the
    pull request's actual target branch; when GitHub does not report a valid
    one the review fails before any workspace or session work.
    """
    denied = policy_denied(REPOSITORY_ACCESS, repository, review_id=review_id)
    if denied is not None:
        _fail_review(review_id, f"blocked by repository policy: {denied.reason}")
        return
    if not _review_awaits_launch(review_id):
        log_event(logger, logging.WARNING, "review.launch_rejected", review_id=review_id, reason="row already bound")
        return
    if _review_refusal(review_id) is not None:
        return
    droid = get_droid_client()
    launch = resolve_agent_launch_config(repository=repository, repository_url=repository_url)
    base_ref = await _pull_request_base_ref(repository, pr_number)
    if base_ref is None:
        _fail_review(review_id, "review not started: the pull request's actual base branch could not be determined")
        log_event(logger, logging.WARNING, "review.base_unknown", review_id=review_id, repo=repository, pr=pr_number)
        return

    try:
        context = await droid.prepare_review_context(
            repository=repository, review_id=review_id, pr_number=pr_number, base_ref=base_ref
        )
    except Exception as exc:
        _fail_review(review_id, f"review context could not be prepared: {safe_error(exc)}")
        log_event(logger, logging.ERROR, "droid.review_context_failed", review_id=review_id, error=str(exc))
        return
    await run_review_over_context(
        review_id,
        context,
        repository=repository,
        repository_url=repository_url or f"https://github.com/{repository}",
        pr_url=pr_url or f"https://github.com/{repository}/pull/{pr_number}",
        model=launch.get("model"),
    )


async def run_review_over_context(
    review_id: int,
    context: ReviewContext,
    *,
    repository: str,
    repository_url: str,
    pr_url: str,
    model: str | None,
) -> None:
    """Bind an exact review context to a still-unbound row and open its fresh restricted session.

    Shared by pull request reviews and pre-publication candidate reviews; an
    incomplete context fails the row before any session is opened.
    """
    droid = get_droid_client()
    if not _persist_review_context(review_id, context):
        log_event(logger, logging.WARNING, "review.launch_rejected", review_id=review_id, reason="row already bound")
        return
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

    binding = ReviewBinding.from_context(context)
    try:
        attempt_id = start_attempt(
            PARENT_REVIEW,
            review_id,
            AttemptKind.REVIEW,
            requested_model=model or getattr(droid, "model", None) or get_settings().droid_model,
            base_sha=context.base_sha,
            head_sha=context.head_sha,
        ).attempt_id
    except (AttemptConflictError, LookupError, RunRefusedError) as exc:
        log_event(logger, logging.WARNING, "review.launch_rejected", review_id=review_id, reason=str(exc))
        return
    try:
        result = await droid.start_review_run(
            context=context,
            repository_url=repository_url,
            review_id=review_id,
            pr_url=pr_url,
            model=model,
            on_complete=partial(finalize_review_task, review_id, binding, attempt_id=attempt_id),
            on_session_opened=partial(_bind_review_session, review_id, binding, attempt_id=attempt_id),
            observer=TurnRecorder(attempt_id),
        )
    except Exception as exc:
        error = f"Droid review session failed to start: {safe_error(exc)}"
        if fail_attempt_start(attempt_id, error):
            _fail_review(review_id, error)
        log_event(
            logger,
            logging.ERROR,
            "droid.review_start_failed",
            review_id=review_id,
            repo=repository,
            error=str(exc),
        )
        return

    _bind_review_session(review_id, binding, result["session_id"], attempt_id=attempt_id)
    log_event(
        logger,
        logging.INFO,
        "droid.review_running",
        review_id=review_id,
        attempt_id=attempt_id,
        session_id=result["session_id"],
        repo=repository,
        head_sha=context.head_sha,
    )


@dataclass(frozen=True)
class _ReviewTarget:
    review_id: int
    repository: str
    pr_number: int
    head_sha: str | None


def review_display_body(text: str) -> str:
    """The review text as posted to GitHub: redacted first, then bounded.

    Bounding first could cut a credential in half so that neither part matches
    the redaction rules, leaving a partial secret in the posted body.
    """
    return redact_text(text)[:REVIEW_BODY_LIMIT]


async def _post_review_verdict(github: GitHubClient, target: _ReviewTarget, text: str, event: str) -> tuple[bool, str]:
    """Post a PR review pinned to the reviewed commit, falling back to COMMENT when GitHub rejects the verdict.

    Returns ``(posted, event)``.
    """
    if policy_denied(OutwardAction.PULL_REQUEST_REVIEW, target.repository, review_id=target.review_id):
        return False, event
    body = review_display_body(text)
    try:
        await github.create_pull_request_review(
            target.repository, target.pr_number, body, event=event, commit_id=target.head_sha
        )
        return True, event
    except httpx.HTTPStatusError as exc:
        # GitHub rejects APPROVE / REQUEST_CHANGES on the token owner's own PRs.
        if exc.response.status_code in (403, 422):
            try:
                await github.create_pull_request_review(
                    target.repository, target.pr_number, body, event="COMMENT", commit_id=target.head_sha
                )
                return True, "COMMENT"
            except Exception as fallback_exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "github.review_post_failed",
                    review_id=target.review_id,
                    error=str(fallback_exc),
                )
        else:
            log_event(
                logger,
                logging.WARNING,
                "github.review_post_failed",
                review_id=target.review_id,
                error=str(exc),
            )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "github.review_post_failed",
            review_id=target.review_id,
            error=str(exc),
        )
    return False, event


async def _pull_request_head_matches(github: GitHubClient, target: _ReviewTarget) -> bool:
    """True only when GitHub confirms the PR head is still the reviewed commit."""
    try:
        pr = await github.get_pull_request(target.repository, target.pr_number)
    except Exception as exc:
        log_event(logger, logging.WARNING, "review.head_check_failed", review_id=target.review_id, error=str(exc))
        return False
    head = (pr.get("head") or {}).get("sha") if isinstance(pr, dict) else None
    if not target.head_sha or head != target.head_sha:
        log_event(
            logger,
            logging.WARNING,
            "review.head_moved",
            review_id=target.review_id,
            reviewed=target.head_sha,
            current=head,
        )
        return False
    return True


def _callback_rejection(review: ReviewTask, binding: ReviewBinding, session_id: object) -> str | None:
    """Why a completion callback may not finalize this row, or ``None`` if it may."""
    if review.status not in TaskStatus.ACTIVE:
        return "review is no longer active"
    if not isinstance(session_id, str) or not session_id or session_id != review.droid_session_id:
        return "completing session is not the session bound to this review"
    if not _bound_to(review, binding):
        return "review context changed since this session started"
    return None


async def finalize_review_task(
    review_id: int, binding: ReviewBinding, outcome: dict[str, Any], *, attempt_id: int
) -> None:
    """Turn-completion handler for one restricted Droid PR review session.

    ``binding`` is the context the completing session was started over. A
    callback from any other session, for another context, for an attempt that
    is no longer active, or for a row that is already terminal changes
    nothing, counts no usage and makes no GitHub call.

    The structured verdict is persisted with the reviewed SHA. Policy is then
    rechecked before any GitHub read or write; only a restricted review of a
    complete diff whose head still matches GitHub posts APPROVE, pinned to the
    bound head. Nothing here merges or enables auto-merge.
    """
    github = GitHubClient()
    status = map_outcome_status(outcome)
    summary = (outcome.get("text") or "").strip() or None
    session_id = outcome.get("session_id")

    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is None:
            log_event(logger, logging.WARNING, "droid.review_missing", review_id=review_id)
            return
        attempt = claim_active_attempt(session, attempt_id, PARENT_REVIEW, review_id)
        rejection = (
            "attempt is not active for this review"
            if attempt is None
            else _callback_rejection(review, binding, session_id)
        )
        if rejection is not None or attempt is None:
            log_event(
                logger,
                logging.WARNING,
                "review.callback_rejected",
                review_id=review_id,
                attempt_id=attempt_id,
                session_id=session_id,
                bound_session_id=review.droid_session_id,
                reason=rejection,
                credits=outcome.get("credits"),
            )
        else:
            old_status = review.status
            _apply_outcome_to_task(session, review, attempt, outcome, status, summary)
            if status == TaskStatus.COMPLETED and summary:
                parsed = effective_verdict(
                    parse_review_output(summary),
                    review_policy=binding.review_policy,
                    diff_complete=binding.diff_complete,
                    reviewed_sha=binding.head_sha,
                )
                review.verdict = parsed.verdict
                review.findings = json.dumps(parsed.findings)
            verdict = review.verdict
            error = review.error
            local_only = review.review_target == REVIEW_TARGET_CANDIDATE
            will_post = not local_only and status == TaskStatus.COMPLETED and bool(summary)
            # Committed with the AI result, before any GitHub await below.
            if will_post:
                set_github_outcome(review, GitHubReviewOutcome.PENDING)
            else:
                mark_unposted(review)
            target = _ReviewTarget(review_id, review.repository, review.pr_number, binding.head_sha)
    if rejection is not None or attempt is None:
        # A stopped review keeps its outcome; only usage it reports late is retained, once.
        retain_late_usage(attempt_id, outcome)
        return

    log_event(
        logger,
        logging.INFO,
        "review.status_changed",
        review_id=review_id,
        session_id=session_id,
        old=old_status,
        new=status,
    )

    if local_only:
        # A candidate review has no pull request yet; its verdict is local evidence only.
        log_event(logger, logging.INFO, "review.candidate_completed", review_id=review_id, verdict=verdict)
        return
    if not will_post or summary is None:
        log_event(logger, logging.WARNING, "review.failed", review_id=review_id, error=error)
        return
    try:
        posted, event = await _post_pending_review(github, target, verdict, summary)
    except BaseException:
        # Cancelled or broken mid-post: GitHub may or may not have the review. Never re-posted.
        _interrupt_pending_outcome(review_id)
        raise

    log_event(
        logger,
        logging.INFO,
        "review.completed",
        review_id=review_id,
        repo=target.repository,
        pr=target.pr_number,
        verdict=verdict,
        github_event=event,
        posted=posted,
        reviewed_sha=target.head_sha,
    )


async def _post_pending_review(
    github: GitHubClient, target: _ReviewTarget, verdict: str | None, summary: str
) -> tuple[bool, str]:
    """Post a completed review whose outcome is pending and record what reached GitHub; ``(posted, event)``."""
    intended = {ReviewVerdict.APPROVE: "APPROVE", ReviewVerdict.REQUEST_CHANGES: "REQUEST_CHANGES"}.get(
        verdict or "", "COMMENT"
    )
    event = intended
    # Checked before the head read too: stored work outside current policy gets no GitHub call at all.
    if policy_denied(OutwardAction.PULL_REQUEST_REVIEW, target.repository, review_id=target.review_id):
        _record_github_outcome(target.review_id, GitHubReviewOutcome.POLICY_DENIED, None)
        return False, event
    if event == "APPROVE" and not await _pull_request_head_matches(github, target):
        event = "COMMENT"
    attempted = event
    posted, event = await _post_review_verdict(github, target, summary, event)
    kind = classify_github_outcome(intended=intended, attempted=attempted, posted_event=event, posted=posted)
    _record_github_outcome(target.review_id, kind, event if posted else None)
    return posted, event


def _record_github_outcome(review_id: int, outcome: str, event: str | None) -> None:
    """Persist what reached GitHub, separately from the AI verdict."""
    with db_session() as session:
        review = session.get(ReviewTask, review_id)
        if review is not None:
            set_github_outcome(review, outcome, event)


def _interrupt_pending_outcome(review_id: int) -> None:
    try:
        with db_session() as session:
            review = session.get(ReviewTask, review_id)
            if review is not None and interrupt_pending(review):
                log_event(logger, logging.WARNING, "review.github_outcome_unconfirmed", review_id=review_id)
    except Exception as exc:
        # Restart recovery bounds a pending outcome that could not be settled here.
        log_event(logger, logging.ERROR, "review.github_outcome_settle_failed", review_id=review_id, error=str(exc))


# ---------------------------------------------------------------------------
# Shared row helpers
# ---------------------------------------------------------------------------


def _mark_row_failed[TaskOrReviewRow: Task | ReviewTask](
    model: type[TaskOrReviewRow], row_id: int, error: str, *, stamp_completed: bool = False
) -> None:
    """Mark a Task/ReviewTask row failed with the given error message."""
    with db_session() as session:
        row = session.get(model, row_id)
        # Only work still waiting or running fails; stopped and blocked outcomes are kept.
        if row is not None and row.status in TaskStatus.ACTIVE:
            row.status = TaskStatus.FAILED
            row.error = error
            if stamp_completed:
                row.completed_at = datetime.now(UTC)
