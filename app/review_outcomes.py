"""What a review's verdict became on GitHub, kept apart from the AI verdict itself.

An AI ``APPROVE`` is the reviewer's opinion. Whether GitHub recorded a formal
review, only a comment (for example because GitHub refuses approvals of the
token owner's own pull request), or nothing at all is a separate fact. A
candidate review runs before anything is published and is never posted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import ReviewTask

REVIEW_TARGET_CANDIDATE = "candidate"
REVIEW_TARGET_PULL_REQUEST = "pull_request"

FORMAL_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES"})


class GitHubReviewOutcome:
    # The AI review finished and its GitHub post is in flight. Persisted together
    # with the AI result, before any GitHub request, so a reader never mistakes
    # an unfinished post for a review that predates outcome tracking (``None``).
    PENDING = "pending"
    # A pending post was cut short (cancellation, unexpected error or restart)
    # before its result was recorded. Whether it reached GitHub is unconfirmed,
    # and it is never retried automatically.
    INTERRUPTED = "interrupted"
    # The review covered a local candidate; there was no pull request to post to.
    LOCAL_ONLY = "local_only"
    # GitHub recorded a formal APPROVE / REQUEST_CHANGES review.
    FORMAL_REVIEW = "formal_review"
    # The verdict was inconclusive, so only a COMMENT was intended and posted.
    COMMENT = "comment"
    # GitHub refused the formal verdict (e.g. self-review) and it was re-posted as COMMENT.
    COMMENT_FALLBACK = "comment_fallback"
    # An APPROVE was downgraded to COMMENT because the PR head moved after the review.
    COMMENT_HEAD_MOVED = "comment_head_moved"
    # Outward-action policy refused the post; nothing reached GitHub.
    POLICY_DENIED = "policy_denied"
    # The post was attempted and failed; nothing is known to have reached GitHub.
    POST_FAILED = "post_failed"
    # The review did not complete with output, so nothing was posted.
    NOT_POSTED = "not_posted"


_LABELS = {
    GitHubReviewOutcome.PENDING: "Pending: the AI review finished and the GitHub post has not been recorded yet",
    GitHubReviewOutcome.INTERRUPTED: (
        "Unconfirmed: posting to GitHub was interrupted before its result was recorded. It may or may not "
        "have reached GitHub and is not retried automatically; check the pull request on GitHub"
    ),
    GitHubReviewOutcome.LOCAL_ONLY: "Local candidate review; not posted to GitHub",
    GitHubReviewOutcome.FORMAL_REVIEW: "Formal GitHub review",
    GitHubReviewOutcome.COMMENT: "Posted as a GitHub comment (no formal verdict)",
    GitHubReviewOutcome.COMMENT_FALLBACK: "Posted as a GitHub comment; GitHub refused the formal verdict, so this is not a formal approval",
    GitHubReviewOutcome.COMMENT_HEAD_MOVED: "Posted as a GitHub comment; the PR head moved after review, so it is not a formal approval",
    GitHubReviewOutcome.POLICY_DENIED: "Not posted; GitHub writes are not permitted by policy",
    GitHubReviewOutcome.POST_FAILED: "Not posted; the GitHub request failed",
    GitHubReviewOutcome.NOT_POSTED: "Not posted; the review produced no result",
}


def classify_github_outcome(*, intended: str, attempted: str, posted_event: str | None, posted: bool) -> str:
    """Classify a PR review post.

    ``intended`` is the event the AI verdict maps to, ``attempted`` the event
    first sent (an APPROVE may be downgraded when the head moved) and
    ``posted_event`` what GitHub finally accepted.
    """
    if not posted:
        return GitHubReviewOutcome.POST_FAILED
    if posted_event in FORMAL_EVENTS:
        return GitHubReviewOutcome.FORMAL_REVIEW
    if intended in FORMAL_EVENTS and attempted == "COMMENT":
        return GitHubReviewOutcome.COMMENT_HEAD_MOVED
    if attempted in FORMAL_EVENTS:
        return GitHubReviewOutcome.COMMENT_FALLBACK
    return GitHubReviewOutcome.COMMENT


def github_outcome_dict(outcome: str | None, event: str | None, recorded_at: str | None) -> dict[str, Any]:
    kind = outcome or "unknown"
    return {
        "kind": kind,
        "event": event,
        "label": _LABELS.get(kind, "Not recorded (review predates outcome tracking)"),
        "formal_github_review": kind == GitHubReviewOutcome.FORMAL_REVIEW,
        "formal_github_approval": kind == GitHubReviewOutcome.FORMAL_REVIEW and event == "APPROVE",
        "pending": kind == GitHubReviewOutcome.PENDING,
        "recorded_at": recorded_at,
    }


def set_github_outcome(review: ReviewTask, outcome: str, event: str | None = None) -> None:
    review.github_review_outcome = outcome
    review.github_review_event = event
    review.github_review_at = datetime.now(UTC)


def mark_unposted(review: ReviewTask) -> None:
    """Record that a review ending without a post reached nothing on GitHub; a recorded outcome is kept."""
    if review.github_review_outcome is not None:
        return
    local = review.review_target == REVIEW_TARGET_CANDIDATE
    set_github_outcome(review, GitHubReviewOutcome.LOCAL_ONLY if local else GitHubReviewOutcome.NOT_POSTED)


def interrupt_pending(review: ReviewTask) -> bool:
    """Bound a pending post that will not finish in this process; ``True`` when it was pending."""
    if review.github_review_outcome != GitHubReviewOutcome.PENDING:
        return False
    set_github_outcome(review, GitHubReviewOutcome.INTERRUPTED)
    return True


__all__ = [
    "FORMAL_EVENTS",
    "REVIEW_TARGET_CANDIDATE",
    "REVIEW_TARGET_PULL_REQUEST",
    "GitHubReviewOutcome",
    "classify_github_outcome",
    "github_outcome_dict",
    "interrupt_pending",
    "mark_unposted",
    "set_github_outcome",
]
