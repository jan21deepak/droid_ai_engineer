"""A PR review's GitHub outcome is explicitly pending between AI completion and the recorded post.

The AI result and ``pending`` are committed together before the first GitHub
request, so a reader polling in between sees "pending" rather than a historical
unknown. Cancellation, unexpected errors and restarts bound it as an
unconfirmed ``interrupted`` outcome; nothing is ever re-posted automatically.
"""

import asyncio

import httpx
import pytest

from app import recovery, run_control, worker
from app.config import get_settings
from app.database import db_session
from app.models import ReviewTask, TaskStatus
from app.review_outcomes import REVIEW_TARGET_CANDIDATE, GitHubReviewOutcome
from app.run_models import PARENT_REVIEW, AttemptStatus, RunAttempt
from tests import test_review_session_binding as prs
from tests.test_review_session_binding import RecordingGitHub, add_review, attempt_of, binding, outcome, row
from tests.test_worker import allow_concurrent_seed_runs


def stored_attempt(attempt_id):
    with db_session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        return attempt.status, attempt.factory_credits


class ObservingGitHub(RecordingGitHub):
    """Snapshots the persisted review row at every GitHub request, before it is answered."""

    def __init__(self, review_id, *, attempt_id, fail_with=None, **kwargs):
        super().__init__(**kwargs)
        self.review_id = review_id
        self.attempt_id = attempt_id
        self.fail_with = fail_with
        self.seen: list[tuple[str, dict, tuple]] = []

    def _observe(self, call):
        self.seen.append((call, row(self.review_id), stored_attempt(self.attempt_id)))

    async def get_pull_request(self, repository, pr_number):
        self._observe("get_pull_request")
        return await super().get_pull_request(repository, pr_number)

    async def create_pull_request_review(self, repository, pr_number, body, *, event="COMMENT", commit_id=None):
        self._observe(f"create_pull_request_review:{event}")
        if self.fail_with is not None:
            raise self.fail_with
        return await super().create_pull_request_review(repository, pr_number, body, event=event, commit_id=commit_id)


async def finalize(review_id, text=None):
    await worker.finalize_review_task(review_id, binding(), outcome("sess-1", text), attempt_id=attempt_of(review_id))


class TestPendingBeforeFirstGitHubRequest:
    @pytest.mark.asyncio
    async def test_formal_approval_is_pending_until_the_post_is_recorded(self, monkeypatch, writes):
        review_id = add_review()
        github = ObservingGitHub(review_id, attempt_id=attempt_of(review_id))
        prs.install(monkeypatch, github)

        await finalize(review_id)

        assert [call for call, _, _ in github.seen] == ["get_pull_request", "create_pull_request_review:APPROVE"]
        for _, during, attempt in github.seen:
            # The AI result, its usage and the explicit pending outcome were committed before any GitHub await.
            assert (during["status"], during["ai_verdict"]) == ("completed", "APPROVE")
            assert during["github_outcome"]["kind"] == GitHubReviewOutcome.PENDING
            assert during["github_outcome"]["pending"] is True
            assert during["github_outcome"]["formal_github_approval"] is False
            assert attempt == (AttemptStatus.COMPLETED, 1.5)
        final = row(review_id)["github_outcome"]
        assert (final["kind"], final["event"], final["pending"], final["formal_github_approval"]) == (
            "formal_review",
            "APPROVE",
            False,
            True,
        )

    @pytest.mark.asyncio
    async def test_comment_fallback_follows_pending(self, monkeypatch, writes):
        review_id = add_review()
        github = ObservingGitHub(review_id, attempt_id=attempt_of(review_id), review_error_status=422)
        prs.install(monkeypatch, github)

        await finalize(review_id)

        assert {seen["github_outcome"]["kind"] for _, seen, _ in github.seen} == {"pending"}
        final = row(review_id)["github_outcome"]
        assert (final["kind"], final["event"], final["formal_github_approval"]) == (
            "comment_fallback",
            "COMMENT",
            False,
        )

    @pytest.mark.asyncio
    async def test_failed_post_follows_pending(self, monkeypatch, writes):
        review_id = add_review()
        error = httpx.ConnectError("network down")
        github = ObservingGitHub(review_id, attempt_id=attempt_of(review_id), fail_with=error)
        prs.install(monkeypatch, github)

        await finalize(review_id)

        assert {seen["github_outcome"]["kind"] for _, seen, _ in github.seen} == {"pending"}
        final = row(review_id)
        assert (final["github_outcome"]["kind"], final["github_outcome"]["pending"]) == ("post_failed", False)
        assert final["ai_verdict"] == "APPROVE"


class TestPendingIsBounded:
    @pytest.mark.asyncio
    async def test_cancellation_during_the_post_is_unconfirmed_and_not_replayed(self, monkeypatch, writes):
        review_id = add_review()
        attempt_id = attempt_of(review_id)
        github = ObservingGitHub(review_id, attempt_id=attempt_id, fail_with=asyncio.CancelledError())
        prs.install(monkeypatch, github)

        with pytest.raises(asyncio.CancelledError):
            await finalize(review_id)

        stored = row(review_id)
        assert stored["github_outcome"]["kind"] == GitHubReviewOutcome.INTERRUPTED
        assert stored["github_outcome"]["pending"] is False
        assert "Unconfirmed" in stored["github_outcome"]["label"]
        assert stored["github_outcome"]["formal_github_approval"] is False
        assert (stored["status"], stored["ai_verdict"]) == ("completed", "APPROVE")
        assert stored_attempt(attempt_id) == (AttemptStatus.COMPLETED, 1.5)
        posts = len(github.calls)

        # A duplicate callback is still rejected: no second post, nothing changes.
        await finalize(review_id)
        assert len(github.calls) == posts
        assert row(review_id) == stored

    @pytest.mark.asyncio
    async def test_unexpected_error_after_ai_completion_is_unconfirmed(self, monkeypatch, writes):
        review_id = add_review()
        prs.install(monkeypatch, RecordingGitHub())

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(worker, "_post_review_verdict", broken)
        with pytest.raises(RuntimeError):
            await finalize(review_id)

        stored = row(review_id)
        assert stored["github_outcome"]["kind"] == GitHubReviewOutcome.INTERRUPTED
        assert (stored["status"], stored["ai_verdict"]) == ("completed", "APPROVE")

    @pytest.mark.asyncio
    async def test_restart_settles_completed_pending_rows_without_posting(self, monkeypatch, writes):
        review_id = add_review(status=TaskStatus.COMPLETED, verdict="APPROVE", github_review_outcome="pending")
        legacy_id = add_review(status=TaskStatus.COMPLETED, verdict="APPROVE", pr_number=10)
        posted_id = add_review(
            status=TaskStatus.COMPLETED,
            pr_number=11,
            github_review_outcome="formal_review",
            github_review_event="APPROVE",
        )
        github = RecordingGitHub()
        prs.install(monkeypatch, github)
        droid = prs.ReviewDroid()

        assert await recovery.recover_interrupted_tasks(droid) == 0

        stored = row(review_id)
        assert (stored["status"], stored["ai_verdict"]) == ("completed", "APPROVE")
        assert stored["github_outcome"]["kind"] == GitHubReviewOutcome.INTERRUPTED
        # Historical unknown and recorded outcomes are left exactly as they were.
        assert row(legacy_id)["github_outcome"]["kind"] == "unknown"
        assert row(posted_id)["github_outcome"]["kind"] == "formal_review"
        assert github.calls == [] and droid.review_runs == []

    @pytest.mark.asyncio
    async def test_restart_orphans_are_not_posted_or_local_only(self, monkeypatch, writes):
        allow_concurrent_seed_runs(monkeypatch)
        pr_review = add_review(droid_session_id="sess-orphan")
        candidate = add_review(
            droid_session_id="sess-candidate", pr_number=0, review_target=REVIEW_TARGET_CANDIDATE, pr_url=""
        )
        await recovery.recover_interrupted_tasks(prs.ReviewDroid())
        assert (row(pr_review)["status"], row(pr_review)["github_outcome"]["kind"]) == ("failed", "not_posted")
        assert row(candidate)["github_outcome"]["kind"] == "local_only"


class TestSettledOutcomesDoNotLookPending:
    @pytest.mark.asyncio
    async def test_candidate_review_is_local_only_with_its_ai_result(self, monkeypatch, writes):
        review_id = add_review(pr_number=0, review_target=REVIEW_TARGET_CANDIDATE, pr_url="")
        github = RecordingGitHub()
        prs.install(monkeypatch, github)
        await finalize(review_id)
        outcome_ = row(review_id)["github_outcome"]
        assert (outcome_["kind"], outcome_["pending"]) == ("local_only", False)
        assert github.calls == []

    @pytest.mark.asyncio
    async def test_unsuccessful_review_is_not_posted(self, monkeypatch, writes):
        review_id = add_review()
        github = RecordingGitHub()
        prs.install(monkeypatch, github)
        failed = {**outcome("sess-1"), "success": False, "subtype": "error_during_execution", "text": ""}
        await worker.finalize_review_task(review_id, binding(), failed, attempt_id=attempt_of(review_id))
        stored = row(review_id)
        assert (stored["status"], stored["github_outcome"]["kind"]) == ("failed", "not_posted")
        assert github.calls == []

    def test_start_failure_is_not_posted(self):
        review_id = add_review(status=TaskStatus.QUEUED, droid_session_id=None)
        worker._fail_review(review_id, "review not started: base unknown")
        stored = row(review_id)
        assert (stored["status"], stored["github_outcome"]["kind"]) == ("failed", "not_posted")

    @pytest.mark.asyncio
    async def test_stopped_review_is_not_posted(self, monkeypatch):
        review_id = add_review()

        class Droid:
            async def interrupt_run(self, session_id):
                return {"interrupt_requested": True}

        monkeypatch.setattr(run_control, "get_droid_client", lambda: Droid())
        await run_control.stop_parent(PARENT_REVIEW, review_id)
        stored = row(review_id)
        assert (stored["status"], stored["github_outcome"]["kind"]) == ("stopped", "not_posted")

    def test_legacy_null_stays_historical_unknown(self):
        review_id = add_review(status=TaskStatus.COMPLETED, verdict="APPROVE")
        outcome_ = row(review_id)["github_outcome"]
        assert (outcome_["kind"], outcome_["pending"]) == ("unknown", False)
        assert "predates" in outcome_["label"]


@pytest.fixture
def writes(monkeypatch):
    monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "true")
    monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", prs.REPO)
    get_settings.cache_clear()


def test_review_detail_api_reports_pending_separately_from_the_ai_status(client):
    with db_session() as session:
        review = ReviewTask(
            repository=prs.REPO,
            pr_number=1,
            status=TaskStatus.COMPLETED,
            verdict="APPROVE",
            github_review_outcome="pending",
        )
        session.add(review)
        session.flush()
        review_id = review.id
    body = client.get(f"/api/reviews/{review_id}").json()["review"]
    assert (body["status"], body["ai_verdict"]) == ("completed", "APPROVE")
    assert body["github_outcome"]["pending"] is True
    assert body["github_outcome"]["formal_github_approval"] is False
    assert "Pending" in body["github_outcome"]["label"]
