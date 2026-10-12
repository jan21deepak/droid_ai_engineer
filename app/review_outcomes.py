"""What a review's verdict became on GitHub, kept apart from the AI verdict itself.

An AI ``APPROVE`` is the reviewer's opinion. Whether GitHub recorded a formal
review, only a comment (for example because GitHub refuses approvals of the
token owner's own pull request), or nothing at all is a separate fact. A
candidate review runs before anything is published and is never posted.
"""

from __future__ import annotations

from typing import Any

REVIEW_TARGET_CANDIDATE = "candidate"
REVIEW_TARGET_PULL_REQUEST = "pull_request"

FORMAL_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES"})


class GitHubReviewOutcome:
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
        "recorded_at": recorded_at,
    }


__all__ = [
    "FORMAL_EVENTS",
    "REVIEW_TARGET_CANDIDATE",
    "REVIEW_TARGET_PULL_REQUEST",
    "GitHubReviewOutcome",
    "classify_github_outcome",
    "github_outcome_dict",
]
