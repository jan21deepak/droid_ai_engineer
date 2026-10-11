"""Review launch/finalization binding: actual PR base, one session per row, bound callbacks, outbound redaction."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app import worker
from app.config import get_settings
from app.database import db_session
from app.models import Repository, ReviewTask, TaskStatus
from app.review_policy import REVIEW_POLICY_RESTRICTED, ReviewBinding, ReviewContext, ReviewVerdict, review_base_ref

REPO = "jan21deepak/omnigent"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
BASE = "c" * 40
SENTINEL = "forge-fake-sentinel-0123456789abcdef"


class RecordingGitHub:
    """Records every GitHub call; any unexpected method fails loudly."""

    def __init__(self, pull=None, base_error=None, review_error_status=None):
        self.pull = pull if pull is not None else {"state": "open", "head": {"sha": HEAD_A}, "base": {"ref": "main"}}
        self.base_error = base_error
        self.review_error_status = review_error_status
        self.calls: list[str] = []
        self.review_posts: list[tuple] = []

    def __getattr__(self, name):
        async def unexpected(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"unexpected GitHub call: {name}")

        return unexpected

    async def get_pull_request(self, repository, pr_number):
        self.calls.append("get_pull_request")
        if self.base_error is not None:
            raise self.base_error
        return self.pull

    async def create_pull_request_review(self, repository, pr_number, body, *, event="COMMENT", commit_id=None):
        self.calls.append("create_pull_request_review")
        if self.review_error_status and event != "COMMENT":
            request = httpx.Request("POST", f"https://api.github.com/repos/{repository}/pulls/{pr_number}/reviews")
            raise httpx.HTTPStatusError(
                "rejected", request=request, response=httpx.Response(self.review_error_status, request=request)
            )
        self.review_posts.append((repository, pr_number, body, event, commit_id))
        return {"id": len(self.review_posts)}


class ReviewDroid:
    """DroidClient stand-in: deterministic contexts and fake restricted sessions (no SDK)."""

    configured = True

    def __init__(self, heads=(HEAD_A,)):
        self.heads = list(heads)
        self.context_requests: list[dict] = []
        self.review_runs: list[dict] = []
        self.launches: list = []

    @property
    def active_sessions(self):
        return set()

    def track(self, key, task):
        # No registry: the active-row guard must hold without it.
        self.launches.append(task)
        return task

    async def prepare_review_context(self, **kwargs):
        self.context_requests.append(kwargs)
        head = self.heads[min(len(self.context_requests), len(self.heads)) - 1]
        return ReviewContext(
            workspace=Path(f"/tmp/review-{kwargs['review_id']}"),
            base_ref=kwargs["base_ref"],
            base_sha=BASE,
            head_sha=head,
            diff="+change\n",
        )

    async def start_review_run(self, **kwargs):
        self.review_runs.append(kwargs)
        session_id = f"sess-{len(self.review_runs)}"
        if kwargs.get("on_session_opened") is not None:
            kwargs["on_session_opened"](session_id)
        return {"session_id": session_id, "status": "running"}


@pytest.fixture
def writes(monkeypatch):
    monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "true")
    monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", REPO)
    monkeypatch.setenv("FORGE_TEST_REVIEW_SECRET", SENTINEL)
    get_settings.cache_clear()


def review_text(verdict="APPROVE", summary="Assessment.", extra=""):
    payload = {"verdict": verdict, "summary": summary, "findings": []}
    return f"{extra}Notes.\n```json\n{json.dumps(payload)}\n```\nVERDICT: {verdict}"


def outcome(session_id, text=None):
    return {
        "session_id": session_id,
        "kind": "review",
        "success": True,
        "subtype": "success",
        "text": text if text is not None else review_text(),
        "duration_seconds": 30.0,
        "credits": 1.5,
        "tokens": None,
        "error": None,
    }


def add_review(**overrides):
    values = {
        "repository": REPO,
        "repository_url": f"https://github.com/{REPO}",
        "pr_number": 9,
        "pr_title": "Fix",
        "pr_url": f"https://github.com/{REPO}/pull/9",
        "droid_session_id": "sess-1",
        "status": TaskStatus.RUNNING,
        "commit_sha": HEAD_A,
        "base_sha": BASE,
        "diff_complete": True,
        "review_policy": REVIEW_POLICY_RESTRICTED,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    with db_session() as session:
        review = ReviewTask(**values)
        session.add(review)
        session.flush()
        return review.id


def binding(head=HEAD_A):
    return ReviewBinding(base_sha=BASE, head_sha=head, diff_complete=True, review_policy=REVIEW_POLICY_RESTRICTED)


def row(review_id):
    with db_session() as session:
        return session.get(ReviewTask, review_id).to_dict()


def install(monkeypatch, github, droid=None):
    monkeypatch.setattr(worker, "GitHubClient", lambda: github)
    if droid is not None:
        monkeypatch.setattr(worker, "get_droid_client", lambda: droid)


async def launch(review_id):
    await worker.launch_review_task(review_id, repository=REPO, repository_url="", pr_number=9, pr_url="")


class TestOutboundRedaction:
    @pytest.mark.parametrize(
        ("verdict", "error_status", "expected_event"),
        [
            ("APPROVE", None, "APPROVE"),
            ("REQUEST_CHANGES", None, "REQUEST_CHANGES"),
            ("REQUEST_CHANGES", 422, "COMMENT"),
        ],
        ids=["approve", "request_changes", "comment_fallback"],
    )
    @pytest.mark.asyncio
    async def test_every_posted_verdict_body_is_redacted(
        self, monkeypatch, writes, verdict, error_status, expected_event
    ):
        review_id = add_review()
        github = RecordingGitHub(review_error_status=error_status)
        install(monkeypatch, github)

        text = review_text(verdict, summary=f"leaked {SENTINEL}", extra=f"token {SENTINEL}\n")
        await worker.finalize_review_task(review_id, binding(), outcome("sess-1", text))

        [(_repo, _pr, body, event, commit_id)] = github.review_posts
        assert (event, commit_id) == (expected_event, HEAD_A)
        assert SENTINEL not in body
        assert "[REDACTED]" in body
        assert SENTINEL not in json.dumps(row(review_id))

    @pytest.mark.asyncio
    async def test_inconclusive_comment_body_is_redacted(self, monkeypatch, writes):
        review_id = add_review()
        github = RecordingGitHub()
        install(monkeypatch, github)

        await worker.finalize_review_task(review_id, binding(), outcome("sess-1", f"No verdict here {SENTINEL}"))

        [(_repo, _pr, body, event, _commit)] = github.review_posts
        assert event == "COMMENT"
        assert SENTINEL not in body
        # INCONCLUSIVE never approves, so the head is not even read.
        assert github.calls == ["create_pull_request_review"]

    @pytest.mark.asyncio
    async def test_sentinel_straddling_the_length_limit_is_redacted_before_truncation(self, monkeypatch, writes):
        monkeypatch.setattr(worker, "REVIEW_BODY_LIMIT", 200)
        review_id = add_review()
        github = RecordingGitHub(review_error_status=422)
        install(monkeypatch, github)

        text = "x" * 180 + SENTINEL + "\n" + review_text("REQUEST_CHANGES")
        await worker.finalize_review_task(review_id, binding(), outcome("sess-1", text))

        [(_repo, _pr, body, event, _commit)] = github.review_posts
        assert event == "COMMENT"
        assert len(body) <= 200
        assert SENTINEL[:20] not in body


class TestFinalizationPolicy:
    @pytest.mark.asyncio
    async def test_changed_allowlist_means_zero_github_reads_or_writes(self, monkeypatch, writes):
        review_id = add_review()
        github = RecordingGitHub()
        install(monkeypatch, github)
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", "someone/else")
        get_settings.cache_clear()

        await worker.finalize_review_task(review_id, binding(), outcome("sess-1", review_text(summary=SENTINEL)))

        assert github.calls == []
        stored = row(review_id)
        # The local result is kept (sanitized) even though nothing is published.
        assert (stored["status"], stored["verdict"], stored["commit_sha"]) == ("completed", "APPROVE", HEAD_A)
        assert SENTINEL not in json.dumps(stored)

    @pytest.mark.asyncio
    async def test_disabled_writes_mean_zero_github_reads(self, monkeypatch, writes):
        review_id = add_review()
        github = RecordingGitHub()
        install(monkeypatch, github)
        monkeypatch.setenv("FORGE_ALLOW_GITHUB_WRITES", "false")
        get_settings.cache_clear()

        await worker.finalize_review_task(review_id, binding(), outcome("sess-1"))

        assert github.calls == []
        assert row(review_id)["verdict"] == ReviewVerdict.APPROVE


class TestActualBase:
    @pytest.mark.parametrize(
        "pull",
        [
            {"state": "open", "head": {"sha": HEAD_A}},
            {"state": "open", "base": {}},
            {"state": "open", "base": {"ref": "   "}},
            {"state": "open", "base": {"ref": 7}},
            {"state": "open", "base": {"ref": "bad..ref"}},
            {"state": "open", "base": {"ref": "-option"}},
            {"state": "open", "base": "main"},
            [],
        ],
        ids=["missing", "no_ref", "blank", "non_string", "dotdot", "option_like", "base_not_object", "not_object"],
    )
    @pytest.mark.asyncio
    async def test_unknown_base_fails_before_context_or_session(self, monkeypatch, writes, pull):
        with db_session() as session:
            session.add(Repository(full_name=REPO, url=f"https://github.com/{REPO}", starting_ref="main"))
        review_id = add_review(status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, base_sha=None)
        github = RecordingGitHub(pull=pull)
        droid = ReviewDroid()
        install(monkeypatch, github, droid)

        await launch(review_id)

        assert droid.context_requests == []
        assert droid.review_runs == []
        stored = row(review_id)
        assert stored["status"] == "failed"
        assert "base" in stored["error"]
        # Never guessed from the configured starting ref or repository default.
        assert github.calls == ["get_pull_request"]

    @pytest.mark.asyncio
    async def test_base_lookup_failure_fails_closed(self, monkeypatch, writes):
        review_id = add_review(status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, base_sha=None)
        github = RecordingGitHub(base_error=httpx.ConnectError(f"boom {SENTINEL}"))
        droid = ReviewDroid()
        install(monkeypatch, github, droid)

        await launch(review_id)

        assert droid.context_requests == [] and droid.review_runs == []
        stored = row(review_id)
        assert stored["status"] == "failed"
        assert SENTINEL not in stored["error"]

    @pytest.mark.asyncio
    async def test_valid_non_default_base_is_honored(self, monkeypatch, writes):
        with db_session() as session:
            session.add(Repository(full_name=REPO, url=f"https://github.com/{REPO}", starting_ref="main"))
        review_id = add_review(status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, base_sha=None)
        pull = {"state": "open", "head": {"sha": HEAD_A}, "base": {"ref": "release/2.x"}}
        droid = ReviewDroid()
        install(monkeypatch, RecordingGitHub(pull=pull), droid)

        await launch(review_id)

        assert [req["base_ref"] for req in droid.context_requests] == ["release/2.x"]
        assert len(droid.review_runs) == 1
        assert row(review_id)["status"] == "running"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("main", "main"),
            ("droid_mission", "droid_mission"),
            ("release/2.x", "release/2.x"),
            (None, None),
            ("", None),
            (" main", None),
            ("a b", None),
            ("x.lock", None),
            ("x/", None),
            ("/x", None),
            ("x//y", None),
            ("a@{1}", None),
            ("@", None),
            ("x~1", None),
            ("x:y", None),
            ("x\x00y", None),
            ("refs/heads/main", None),
        ],
    )
    def test_review_base_ref_accepts_only_plain_branch_names(self, value, expected):
        assert review_base_ref(value) == expected


class TestSessionBinding:
    @pytest.mark.asyncio
    async def test_active_reassignment_is_rejected_without_the_launch_registry(self, monkeypatch, writes):
        droid = ReviewDroid(heads=(HEAD_A, HEAD_B))
        github = RecordingGitHub()
        install(monkeypatch, github, droid)
        launched: list[int] = []

        async def fake_launch(review_id, **kwargs):
            launched.append(review_id)

        monkeypatch.setattr(worker, "launch_review_task", fake_launch)

        first = await worker.start_pr_review(repository=REPO, pr_number=9)
        second = await worker.start_pr_review(repository=REPO, pr_number=9)
        for task in droid.launches:
            await task

        assert launched == [first["review_id"]]
        assert first["started"] is True
        assert second == {"review_id": first["review_id"], "started": False, "detail": "review_already_active"}
        with db_session() as session:
            assert session.query(ReviewTask).count() == 1

    @pytest.mark.asyncio
    async def test_second_launch_cannot_replace_active_row_context(self, monkeypatch, writes):
        review_id = add_review(status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, base_sha=None)
        droid = ReviewDroid(heads=(HEAD_A, HEAD_B))
        install(monkeypatch, RecordingGitHub(), droid)

        await launch(review_id)
        await launch(review_id)

        assert len(droid.context_requests) == 1
        assert len(droid.review_runs) == 1
        stored = row(review_id)
        assert (stored["status"], stored["droid_session_id"], stored["commit_sha"]) == ("running", "sess-1", HEAD_A)

    @pytest.mark.asyncio
    async def test_session_id_is_persisted_before_the_turn_is_spawned(self, monkeypatch, writes):
        review_id = add_review(status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, base_sha=None)
        seen: list[dict] = []
        droid = ReviewDroid()

        async def start_review_run(**kwargs):
            droid.review_runs.append(kwargs)
            kwargs["on_session_opened"]("sess-early")
            seen.append(row(review_id))
            return {"session_id": "sess-early", "status": "running"}

        droid.start_review_run = start_review_run
        install(monkeypatch, RecordingGitHub(), droid)

        await launch(review_id)

        assert (seen[0]["status"], seen[0]["droid_session_id"]) == ("running", "sess-early")

    @pytest.mark.asyncio
    async def test_callback_for_context_a_cannot_approve_context_b(self, monkeypatch, writes):
        """Reproduction: session A's late callback against a row that now holds B must change nothing."""
        review_id = add_review(status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, base_sha=None)
        droid = ReviewDroid(heads=(HEAD_A,))
        github = RecordingGitHub(pull={"state": "open", "head": {"sha": HEAD_B}, "base": {"ref": "main"}})
        install(monkeypatch, github, droid)
        await launch(review_id)
        on_complete_a = droid.review_runs[0]["on_complete"]
        github.calls.clear()

        # The row is (by whatever path) now carrying a second session over head B.
        with db_session() as session:
            review = session.get(ReviewTask, review_id)
            review.droid_session_id = "sess-B"
            review.commit_sha = HEAD_B
        before = row(review_id)

        await on_complete_a(outcome("sess-1"))

        assert row(review_id) == before
        assert github.calls == []
        assert github.review_posts == []

    @pytest.mark.asyncio
    async def test_mismatched_session_with_same_context_is_rejected(self, monkeypatch, writes):
        review_id = add_review(droid_session_id="sess-current")
        github = RecordingGitHub()
        install(monkeypatch, github)
        before = row(review_id)

        await worker.finalize_review_task(review_id, binding(), outcome("sess-stale"))
        await worker.finalize_review_task(review_id, binding(), {**outcome("sess-x"), "session_id": None})

        assert row(review_id) == before
        assert github.calls == []

    @pytest.mark.asyncio
    async def test_duplicate_callback_does_not_mutate_or_post_again(self, monkeypatch, writes):
        review_id = add_review()
        github = RecordingGitHub()
        install(monkeypatch, github)

        await worker.finalize_review_task(review_id, binding(), outcome("sess-1"))
        after_first = row(review_id)
        await worker.finalize_review_task(
            review_id, binding(), outcome("sess-1", review_text("REQUEST_CHANGES", summary="changed"))
        )

        assert row(review_id) == after_first
        assert [(post[3], post[4]) for post in github.review_posts] == [("APPROVE", HEAD_A)]

    @pytest.mark.asyncio
    async def test_bound_callback_posts_only_the_bound_head(self, monkeypatch, writes):
        review_id = add_review(status=TaskStatus.QUEUED, droid_session_id=None, commit_sha=None, base_sha=None)
        droid = ReviewDroid(heads=(HEAD_A,))
        github = RecordingGitHub()
        install(monkeypatch, github, droid)
        await launch(review_id)

        await droid.review_runs[0]["on_complete"](outcome("sess-1"))

        assert [(post[3], post[4]) for post in github.review_posts] == [("APPROVE", HEAD_A)]
        stored = row(review_id)
        assert (stored["status"], stored["verdict"], stored["commit_sha"]) == ("completed", "APPROVE", HEAD_A)

    @pytest.mark.asyncio
    async def test_terminal_review_reassignment_gets_a_fresh_row_and_finishes(self, monkeypatch, writes):
        droid = ReviewDroid(heads=(HEAD_A, HEAD_B))
        github = RecordingGitHub(pull={"state": "open", "head": {"sha": HEAD_B}, "base": {"ref": "main"}})
        install(monkeypatch, github, droid)

        first = await worker.start_pr_review(repository=REPO, pr_number=9)
        await droid.launches.pop()
        await droid.review_runs[0]["on_complete"](outcome("sess-1", review_text("REQUEST_CHANGES")))
        second = await worker.start_pr_review(repository=REPO, pr_number=9)
        await droid.launches.pop()
        await droid.review_runs[1]["on_complete"](outcome("sess-2"))

        assert second["started"] is True and second["review_id"] != first["review_id"]
        assert [req["review_id"] for req in droid.context_requests] == [first["review_id"], second["review_id"]]
        old, new = row(first["review_id"]), row(second["review_id"])
        assert (old["verdict"], old["commit_sha"], old["droid_session_id"]) == ("REQUEST_CHANGES", HEAD_A, "sess-1")
        assert (new["verdict"], new["commit_sha"], new["droid_session_id"]) == ("APPROVE", HEAD_B, "sess-2")
        assert [(post[3], post[4]) for post in github.review_posts] == [
            ("REQUEST_CHANGES", HEAD_A),
            ("APPROVE", HEAD_B),
        ]
