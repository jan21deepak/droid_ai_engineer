"""AI verdict vs GitHub review outcome, candidate reviews, and remote CI observation stay distinct."""

import httpx
import pytest

from app import candidate_review, publishing, worker
from app.config import get_settings
from app.database import db_session
from app.delivery import approve_candidate, task_delivery
from app.delivery_models import CIState, Publication
from app.models import ReviewTask, TaskStatus
from app.publishing import publish_candidate
from app.remote_ci import derive_ci_state, observe_publication_ci, refresh_published_ci
from app.review_outcomes import GitHubReviewOutcome, classify_github_outcome
from app.review_policy import ReviewVerdict
from app.run_models import PARENT_REVIEW, RunAttempt
from tests import test_review_session_binding as prs
from tests.delivery_fixtures import (
    FakeDeliveryGitHub,
    add_candidate_review,
    add_candidate_task,
    add_check_run,
    configure,
    http_error,
)


class TestClassification:
    @pytest.mark.parametrize(
        ("intended", "attempted", "posted_event", "posted", "expected"),
        [
            ("APPROVE", "APPROVE", "APPROVE", True, GitHubReviewOutcome.FORMAL_REVIEW),
            ("REQUEST_CHANGES", "REQUEST_CHANGES", "REQUEST_CHANGES", True, GitHubReviewOutcome.FORMAL_REVIEW),
            ("APPROVE", "APPROVE", "COMMENT", True, GitHubReviewOutcome.COMMENT_FALLBACK),
            ("APPROVE", "COMMENT", "COMMENT", True, GitHubReviewOutcome.COMMENT_HEAD_MOVED),
            ("COMMENT", "COMMENT", "COMMENT", True, GitHubReviewOutcome.COMMENT),
            ("APPROVE", "APPROVE", None, False, GitHubReviewOutcome.POST_FAILED),
        ],
    )
    def test_matrix(self, intended, attempted, posted_event, posted, expected):
        assert classify_github_outcome(
            intended=intended, attempted=attempted, posted_event=posted_event, posted=posted
        ) == (expected)


class TestPullRequestReviewOutcome:
    @pytest.fixture
    def writes(self, monkeypatch):
        monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "true")
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", prs.REPO)
        get_settings.cache_clear()

    async def finalize(self, monkeypatch, github, verdict="APPROVE"):
        review_id = prs.add_review()
        prs.install(monkeypatch, github)
        await worker.finalize_review_task(
            review_id,
            prs.binding(),
            prs.outcome("sess-1", prs.review_text(verdict)),
            attempt_id=prs.attempt_of(review_id),
        )
        return prs.row(review_id)

    @pytest.mark.asyncio
    async def test_self_review_fallback_is_a_comment_not_a_formal_approval(self, monkeypatch, writes):
        row = await self.finalize(monkeypatch, prs.RecordingGitHub(review_error_status=422))

        assert row["ai_verdict"] == ReviewVerdict.APPROVE
        outcome = row["github_outcome"]
        assert (outcome["kind"], outcome["event"]) == ("comment_fallback", "COMMENT")
        assert outcome["formal_github_review"] is False and outcome["formal_github_approval"] is False
        assert "not a formal approval" in outcome["label"]
        assert row["review_target"] == "pull_request"

    @pytest.mark.asyncio
    async def test_formal_approval_is_recorded_as_such(self, monkeypatch, writes):
        row = await self.finalize(monkeypatch, prs.RecordingGitHub())

        outcome = row["github_outcome"]
        assert (outcome["kind"], outcome["event"], outcome["formal_github_approval"]) == (
            "formal_review",
            "APPROVE",
            True,
        )

    @pytest.mark.asyncio
    async def test_head_moved_downgrade(self, monkeypatch, writes):
        github = prs.RecordingGitHub(pull={"state": "open", "head": {"sha": prs.HEAD_B}, "base": {"ref": "main"}})
        row = await self.finalize(monkeypatch, github)
        assert row["github_outcome"]["kind"] == "comment_head_moved"
        assert row["ai_verdict"] == ReviewVerdict.APPROVE

    @pytest.mark.asyncio
    async def test_policy_denied_posts_nothing(self, monkeypatch):
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", prs.REPO)
        get_settings.cache_clear()
        github = prs.RecordingGitHub()
        row = await self.finalize(monkeypatch, github)
        assert row["github_outcome"]["kind"] == "policy_denied"
        assert github.calls == []


class CandidateDroid:
    """Fake restricted review session; nothing reaches the SDK."""

    configured = True
    model = "auto"

    def __init__(self):
        self.runs: list[dict] = []
        self.launches: list = []

    @property
    def active_sessions(self):
        return set()

    def track(self, key, task):
        self.launches.append(task)
        return task

    def session_busy(self, session_id):
        return False

    async def start_review_run(self, **kwargs):
        self.runs.append(kwargs)
        kwargs["on_session_opened"]("sess-candidate")
        return {"session_id": "sess-candidate", "status": "running"}


class TestCandidateReview:
    @pytest.mark.asyncio
    async def test_reviews_exact_candidate_diff_and_posts_nothing(self, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        task_id, sha, _ws = add_candidate_task(remote)
        droid = CandidateDroid()
        github = prs.RecordingGitHub()
        monkeypatch.setattr(candidate_review, "get_droid_client", lambda: droid)
        monkeypatch.setattr(worker, "get_droid_client", lambda: droid)
        monkeypatch.setattr(worker, "GitHubClient", lambda: github)

        started = await candidate_review.start_candidate_review(task_id)
        await droid.launches[0]

        [run] = droid.runs
        context = run["context"]
        assert (context.head_sha, context.base_sha, context.complete) == (sha, remote.base_sha, True)
        assert "fix.txt" in context.diff
        review_id = started["review_id"]
        with db_session() as session:
            # The attempt was committed before the (fake) session was opened.
            assert (
                session.query(RunAttempt)
                .filter(RunAttempt.parent_type == PARENT_REVIEW, RunAttempt.parent_id == review_id)
                .count()
                == 1
            )
        await run["on_complete"](prs.outcome("sess-candidate", prs.review_text("APPROVE")))

        with db_session() as session:
            review = session.get(ReviewTask, review_id).to_dict()
        assert (review["status"], review["commit_sha"], review["ai_verdict"]) == (
            TaskStatus.COMPLETED,
            sha,
            ReviewVerdict.APPROVE,
        )
        assert (review["review_target"], review["task_id"], review["github_outcome"]["kind"]) == (
            "candidate",
            task_id,
            "local_only",
        )
        assert github.calls == []
        assert task_delivery(task_id)["review"]["status"] == "approving"

    @pytest.mark.asyncio
    async def test_refuses_a_second_active_review(self, monkeypatch, tmp_path):
        from app.delivery import DeliveryRefusedError

        remote = configure(monkeypatch, tmp_path)
        task_id, sha, _ws = add_candidate_task(remote)
        add_candidate_review(task_id, sha, remote.base_sha, status=TaskStatus.RUNNING)
        monkeypatch.setattr(candidate_review, "get_droid_client", lambda: CandidateDroid())
        with pytest.raises(DeliveryRefusedError) as info:
            await candidate_review.start_candidate_review(task_id)
        assert info.value.detail == "review_already_active"


class TestRemoteCiDerivation:
    def run(self, status="completed", conclusion="success"):
        return {"name": "ci", "status": status, "conclusion": conclusion}

    @pytest.mark.parametrize(
        ("runs", "statuses", "expected"),
        [
            ([], [], CIState.NONE_REPORTED),
            ([{"name": "ci", "status": "queued", "conclusion": None}], [], CIState.PENDING),
            ([{"name": "a", "status": "completed", "conclusion": "success"}], [], CIState.PASSING),
            ([{"name": "a", "status": "completed", "conclusion": "skipped"}], [], CIState.PASSING),
            (
                [
                    {"name": "a", "status": "completed", "conclusion": "failure"},
                    {"name": "b", "status": "in_progress", "conclusion": None},
                ],
                [],
                CIState.FAILING,
            ),
            ([{"name": "a", "status": "completed", "conclusion": "cancelled"}], [], CIState.FAILING),
            ([], [{"context": "legacy", "state": "pending"}], CIState.PENDING),
            ([], [{"context": "legacy", "state": "error"}], CIState.FAILING),
        ],
    )
    def test_states_stay_distinct(self, runs, statuses, expected):
        assert derive_ci_state(runs, statuses)[0] == expected


class TestRemoteCiObservation:
    @pytest.fixture
    async def published(self, monkeypatch, tmp_path):
        monkeypatch.setattr(publishing, "PR_HEAD_RECHECK_DELAYS", (0.0,))
        remote = configure(monkeypatch, tmp_path)
        github = FakeDeliveryGitHub(remote)
        task_id, sha, _ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)
        await approve_candidate(task_id, sha, approved_via="bearer")
        result = await publish_candidate(task_id, sha, github=github)
        assert result["published"]
        return task_id, sha, result["publication"]["id"], github

    @pytest.mark.asyncio
    async def test_pending_failing_passing_after_publication(self, published):
        task_id, sha, pub_id, github = published
        assert task_delivery(task_id)["remote_ci"]["state"] == "not_observed"

        github.check_runs = [{"name": "ci", "status": "in_progress", "conclusion": None}]
        assert (await observe_publication_ci(pub_id, github))["state"] == "pending"
        github.check_runs = [{"name": "ci", "status": "completed", "conclusion": "failure", "html_url": "u"}]
        failing = await observe_publication_ci(pub_id, github)
        assert failing["state"] == "failing"
        assert failing["checks"][0]["url"] == "u"
        github.check_runs = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        passing = await observe_publication_ci(pub_id, github)
        assert (passing["state"], passing["observed_sha"], passing["stale"]) == ("passing", sha, False)
        assert passing["observed_at"]
        # CI passing is observation only; it never merges or changes publication.
        state = task_delivery(task_id)
        assert state["publication"]["pull_request"]["merged"] is False
        assert state["merge"]["enabled"] is False

    @pytest.mark.parametrize(
        ("error", "fragment"),
        [
            (http_error(403, "GET"), "HTTP 403"),
            (httpx.ConnectError("unreachable"), "could not be reached"),
        ],
    )
    @pytest.mark.asyncio
    async def test_unavailable_is_never_passing(self, published, error, fragment):
        task_id, _sha, pub_id, github = published
        github.ci_error = error

        observed = await observe_publication_ci(pub_id, github)

        assert observed["state"] == CIState.UNAVAILABLE
        assert fragment in observed["detail"]
        assert task_delivery(task_id)["remote_ci"]["state"] == "unavailable"

    @pytest.mark.asyncio
    async def test_no_reported_checks_is_its_own_state(self, published):
        _task_id, _sha, pub_id, github = published
        observed = await observe_publication_ci(pub_id, github)
        assert observed["state"] == CIState.NONE_REPORTED
        assert "no checks" in observed["detail"]

    @pytest.mark.asyncio
    async def test_poller_skips_merged_or_closed_pull_requests(self, published):
        _task_id, _sha, pub_id, github = published
        assert await refresh_published_ci(github) == 1
        with db_session() as session:
            session.get(Publication, pub_id).pr_state = "closed"
        github.calls.clear()
        assert await refresh_published_ci(github) == 0
        assert github.calls == []
