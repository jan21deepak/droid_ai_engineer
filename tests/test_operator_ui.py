"""Executive overview, task/review detail pages and PR review assignment by number.

Delivery states come from the labeled local Git fixtures in
``tests/delivery_fixtures.py``; GitHub and the Droid SDK are fakes.
"""

from datetime import UTC, datetime
from typing import ClassVar

import pytest

from app import review_routes
from app.config import get_settings
from app.database import db_session
from app.delivery_models import CIState, DeliveryApproval, Publication, PublicationStatus
from app.models import Repository, ReviewTask, SyncedPullRequest, Task, TaskStatus
from app.repository_routes import _persist_synced_pulls
from app.review_outcomes import REVIEW_TARGET_CANDIDATE, GitHubReviewOutcome
from tests.delivery_fixtures import (
    REPO,
    add_candidate_review,
    add_candidate_task,
    add_check_run,
    configure,
    http_error,
    recandidate,
)

HTML = {"Accept": "text/html"}


def add_task(**overrides) -> int:
    values = {
        "repository": REPO,
        "repository_url": f"https://github.com/{REPO}",
        "issue_number": 5,
        "issue_title": "Clarify docs",
        "status": TaskStatus.COMPLETED,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    with db_session() as session:
        task = Task(**values)
        session.add(task)
        session.flush()
        return task.id


def publish_fixture(task_id: int, sha: str, approval_id: int, *, ci: str = CIState.PASSING) -> None:
    """Labeled fixture: a completed publication of ``sha`` with an observed CI state."""
    with db_session() as session:
        session.add(
            Publication(
                task_id=task_id,
                candidate_sha=sha,
                approval_id=approval_id,
                repository=REPO,
                branch="droid_mission",
                base_branch="main",
                status=PublicationStatus.PUBLISHED,
                publish_attempts=1,
                pushed=True,
                push_detail="pushed",
                remote_head_sha=sha,
                pr_number=101,
                pr_url=f"https://github.com/{REPO}/pull/101",
                pr_draft=True,
                pr_state="open",
                pr_head_sha=sha,
                pr_action="created",
                ci_state=ci,
                ci_head_sha=sha,
                ci_observed_at=datetime.now(UTC),
            )
        )


class TestLanding:
    def test_dashboard_opens_on_overview_with_section_navigation(self, client):
        page = client.get("/dashboard")
        assert page.status_code == 200
        text = page.text
        assert 'class="nav-link active" id="overview-tab"' in text
        assert 'id="overview" role="tabpanel"' in text and "show active" in text
        order = [
            text.index(f'id="{name}-tab"') for name in ("overview", "repos", "issues", "reviews", "metrics", "guide")
        ]
        assert order == sorted(order)
        assert "/static/overview.js" in text
        assert "/static/forge_common.js" in text
        assert 'class="tab-pane fade show active" id="repos"' not in text

    def test_activity_rows_link_to_detail_pages(self, client):
        task_id = add_task()
        with db_session() as session:
            session.add(ReviewTask(repository=REPO, pr_number=9, status=TaskStatus.COMPLETED))
        text = client.get("/dashboard").text
        assert f'href="/tasks/{task_id}"' in text
        assert 'href="/reviews/1"' in text
        assert "Agent turn completed" in text

    def test_static_scripts_are_served(self, client):
        for name in ("forge_common.js", "overview.js", "task_detail.js", "review_detail.js", "operator.css"):
            assert client.get(f"/static/{name}").status_code == 200, name


class TestOverview:
    def test_empty_dataset_reports_zero_counts_and_definitions(self, client):
        body = client.get("/api/overview").json()
        assert body["tasks"] == []
        counts = body["counts"]
        for key in ("agent_completed", "awaiting_approval", "accepted", "published", "merged", "needs_attention"):
            assert counts[key] == 0, key
        assert set(counts["remote_ci"].values()) == {0}
        assert "not acceptance" in body["definitions"]["agent_completed"]
        assert "never merges" in body["definitions"]["merged"]
        assert body["merge"]["enabled"] is False

    def test_stages_keep_completion_acceptance_publication_ci_and_merge_apart(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        running = add_task(status=TaskStatus.RUNNING)
        stopped = add_task(status=TaskStatus.STOPPED)
        no_candidate = add_task()
        evidence_id, _, _ = add_candidate_task(remote)

        ready_id, ready_sha, _ = add_candidate_task(remote)
        add_check_run(ready_id, ready_sha)
        add_candidate_review(ready_id, ready_sha, remote.base_sha)

        approved_id, approved_sha, _ = add_candidate_task(remote)
        add_check_run(approved_id, approved_sha)
        add_candidate_review(approved_id, approved_sha, remote.base_sha)
        assert client.post(f"/api/tasks/{approved_id}/approve", json={"candidate_sha": approved_sha}).status_code == 200

        published_id, published_sha, _ = add_candidate_task(remote)
        add_check_run(published_id, published_sha)
        add_candidate_review(published_id, published_sha, remote.base_sha)
        approval = client.post(f"/api/tasks/{published_id}/approve", json={"candidate_sha": published_sha}).json()
        publish_fixture(published_id, published_sha, approval["approval"]["id"])

        body = client.get("/api/overview").json()
        stages = {row["id"]: row["stage"] for row in body["tasks"]}
        assert stages == {
            running: "agent_running",
            stopped: "agent_stopped",
            no_candidate: "no_candidate",
            evidence_id: "awaiting_evidence",
            ready_id: "ready_for_approval",
            approved_id: "approved",
            published_id: "published",
        }
        counts = body["counts"]
        assert counts["agent_completed"] == 5
        assert counts["awaiting_evidence"] == 1
        assert counts["awaiting_approval"] == 1
        assert counts["accepted"] == 2
        assert counts["approved_unpublished"] == 1
        assert counts["published"] == 1
        assert counts["remote_ci"]["passing"] == 1
        assert counts["merged"] == 0
        assert counts["needs_attention"] == 1
        assert counts["agent_running"] == 1
        row = next(r for r in body["tasks"] if r["id"] == published_id)
        assert (row["pull_request_number"], row["remote_ci"], row["candidate_sha"]) == (101, "passing", published_sha)

    def test_merge_is_only_an_observation(self, client):
        add_task(pr_state="merged")
        counts = client.get("/api/overview").json()["counts"]
        assert counts["merged"] == 1
        assert counts["accepted"] == 0

    def test_new_candidate_marks_earlier_approval_stale_and_unaccepted(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        task_id, sha, ws = add_candidate_task(remote)
        add_check_run(task_id, sha)
        add_candidate_review(task_id, sha, remote.base_sha)
        assert client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha}).status_code == 200
        fresh = client.get("/api/overview").json()["tasks"][0]
        assert (fresh["stage"], fresh["approval_stale"]) == ("approved", False)

        new_sha = recandidate(task_id, ws)
        body = client.get("/api/overview").json()
        row = body["tasks"][0]
        assert row["candidate_sha"] == new_sha
        assert (row["stage"], row["approval_stale"]) == ("awaiting_evidence", True)
        assert body["counts"]["accepted"] == 0
        assert "agent_running" in body["definitions"]


class TestDetailPages:
    def test_task_page_escapes_user_supplied_title(self, client):
        task_id = add_task(issue_title="<script>alert('x')</script>")
        page = client.get(f"/tasks/{task_id}")
        assert page.status_code == 200
        assert f'data-task-id="{task_id}"' in page.text
        assert "<script>alert(" not in page.text
        assert "&lt;script&gt;" in page.text
        assert "/static/task_detail.js" in page.text
        assert 'href="/dashboard#reviews"' in page.text

    def test_missing_task_page_is_a_readable_404(self, client):
        page = client.get("/tasks/9999")
        assert page.status_code == 404
        assert "does not exist" in page.text
        assert "/static/task_detail.js" not in page.text

    def test_review_page_shows_shell_and_404(self, client):
        with db_session() as session:
            review = ReviewTask(repository=REPO, pr_number=12, pr_title="<b>bold</b>", status=TaskStatus.COMPLETED)
            session.add(review)
            session.flush()
            review_id = review.id
        page = client.get(f"/reviews/{review_id}")
        assert page.status_code == 200
        assert "PR #12" in page.text and "&lt;b&gt;bold&lt;/b&gt;" in page.text
        assert client.get("/reviews/9999").status_code == 404

    def test_pages_and_overview_require_operator_access(self, protected_client):
        page = protected_client.get("/tasks/1", headers=HTML, follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login?next=")
        assert protected_client.get("/reviews/1", headers=HTML, follow_redirects=False).status_code == 303
        assert protected_client.get("/api/overview").status_code == 401
        assert protected_client.get("/api/reviews").status_code == 401


class TestReviewList:
    def test_lists_pr_reviews_with_head_verdict_and_github_outcome(self, client):
        with db_session() as session:
            session.add(
                ReviewTask(
                    repository=REPO,
                    pr_number=7,
                    status=TaskStatus.COMPLETED,
                    commit_sha="b" * 40,
                    verdict="APPROVE",
                    github_review_outcome=GitHubReviewOutcome.COMMENT_FALLBACK,
                    github_review_event="COMMENT",
                )
            )
            session.add(
                ReviewTask(
                    repository=REPO, pr_number=0, status=TaskStatus.COMPLETED, review_target=REVIEW_TARGET_CANDIDATE
                )
            )
        reviews = client.get("/api/reviews").json()["reviews"]
        assert [r["pr_number"] for r in reviews] == [7]
        review = reviews[0]
        assert review["commit_sha"] == "b" * 40
        assert review["ai_verdict"] == "APPROVE"
        assert review["github_outcome"]["formal_github_approval"] is False
        assert "not a formal approval" in review["github_outcome"]["label"]
        assert len(client.get("/api/reviews?target=all").json()["reviews"]) == 2


class FakeLookupGitHub:
    error: ClassVar[Exception | None] = None
    pull: ClassVar[dict] = {}

    async def get_pull_request(self, repository, pr_number):
        if FakeLookupGitHub.error is not None:
            raise FakeLookupGitHub.error
        return FakeLookupGitHub.pull


class TestAssignByNumber:
    @pytest.fixture
    def starts(self, monkeypatch):
        calls: list[dict] = []

        async def fake_start(**kwargs):
            calls.append(kwargs)
            return {"review_id": 31, "started": True, "detail": "droid_review_started"}

        monkeypatch.setattr(review_routes, "start_pr_review", fake_start)
        monkeypatch.setattr(review_routes, "GitHubClient", FakeLookupGitHub)
        FakeLookupGitHub.error = None
        FakeLookupGitHub.pull = {}
        with db_session() as session:
            session.add(Repository(full_name=REPO, url=f"https://github.com/{REPO}"))
        return calls

    @pytest.mark.parametrize("number", [0, -3, "12", "abc", 1.5, None])
    def test_invalid_identifier_is_rejected_without_starting(self, client, starts, number):
        resp = client.post("/api/pulls/assign-number", json={"repository": REPO, "pr_number": number})
        assert resp.status_code == 422
        assert starts == []

    def test_unregistered_repository_is_rejected(self, client, starts):
        resp = client.post("/api/pulls/assign-number", json={"repository": "someone/else", "pr_number": 3})
        assert (resp.status_code, resp.json()["detail"]) == (404, "repository_not_registered")
        assert starts == []

    def test_repository_outside_policy_is_rejected(self, client, starts, monkeypatch):
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", "other/repo")
        get_settings.cache_clear()
        resp = client.post("/api/pulls/assign-number", json={"repository": REPO, "pr_number": 3})
        assert (resp.status_code, resp.json()["detail"]) == (403, "repository_not_allowed")
        assert starts == []

    def test_unknown_pull_request_is_a_recoverable_404(self, client, starts):
        FakeLookupGitHub.error = http_error(404, "GET")
        resp = client.post("/api/pulls/assign-number", json={"repository": REPO, "pr_number": 999})
        assert (resp.status_code, resp.json()["detail"]) == (404, "pull_request_not_found")
        assert starts == []

    def test_closed_pull_request_is_refused(self, client, starts):
        FakeLookupGitHub.pull = {"state": "closed", "title": "old"}
        resp = client.post("/api/pulls/assign-number", json={"repository": REPO, "pr_number": 4})
        assert (resp.status_code, resp.json()["detail"]) == (409, "pull_request_not_open")
        assert starts == []

    def test_synced_draft_pull_request_starts_one_review(self, client, starts):
        with db_session() as session:
            session.add(
                SyncedPullRequest(
                    repository=REPO,
                    pr_number=42,
                    title="Demo draft",
                    html_url=f"https://github.com/{REPO}/pull/42",
                    head_sha="c" * 40,
                    draft=True,
                )
            )
        resp = client.post("/api/pulls/assign-number", json={"repository": REPO, "pr_number": 42})
        assert resp.status_code == 200
        body = resp.json()
        assert (body["review_id"], body["started"], body["draft"]) == (31, True, True)
        assert len(starts) == 1
        assert (starts[0]["pr_number"], starts[0]["pr_title"]) == (42, "Demo draft")

    def test_already_active_review_reports_its_id(self, client, starts, monkeypatch):
        async def busy_start(**kwargs):
            return {"review_id": 8, "started": False, "detail": "review_already_active"}

        monkeypatch.setattr(review_routes, "start_pr_review", busy_start)
        FakeLookupGitHub.pull = {"state": "open", "title": "t", "html_url": "https://github.com/x/y/pull/5"}
        resp = client.post("/api/pulls/assign-number", json={"repository": REPO, "pr_number": 5})
        assert resp.status_code == 409
        assert (resp.json()["detail"], resp.json()["review_id"]) == ("review_already_active", 8)


def test_pull_sync_keeps_draft_pull_requests():
    count = _persist_synced_pulls(
        REPO,
        f"https://github.com/{REPO}",
        [
            {"number": 5, "title": "ready", "state": "open", "draft": False, "head": {"sha": "a" * 40}},
            {"number": 6, "title": "draft", "state": "open", "draft": True, "head": {"sha": "b" * 40}},
            {"number": 7, "title": "closed", "state": "closed"},
        ],
    )
    assert count == 2
    with db_session() as session:
        rows = {p.pr_number: p.draft for p in session.query(SyncedPullRequest).all()}
    assert rows == {5: False, 6: True}


def test_approval_is_unchanged_by_overview_reads(client, monkeypatch, tmp_path):
    remote = configure(monkeypatch, tmp_path)
    task_id, sha, _ = add_candidate_task(remote)
    add_check_run(task_id, sha)
    add_candidate_review(task_id, sha, remote.base_sha)
    client.get("/api/overview")
    with db_session() as session:
        assert session.query(DeliveryApproval).count() == 0
        assert session.query(Publication).count() == 0
