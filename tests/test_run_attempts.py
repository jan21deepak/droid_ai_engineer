"""Durable per-turn attempts: committed before spawn, finalized once, appended by follow-up/recovery.

The SDK and GitHub are deterministic local fakes. Each fake's turn entry point
is an "SDK entry probe" that records which attempts were already committed at
the moment the turn would have been spawned.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import reconcile, recovery, worker
from app.attempts import parent_accounting
from app.candidates import WorkspaceState, evidence_applies_to_candidate
from app.config import get_settings
from app.database import db_session
from app.droid_client import DroidClient
from app.models import ReviewTask, Task, TaskStatus
from app.review_policy import REVIEW_POLICY_RESTRICTED, ReviewContext
from app.run_models import PARENT_REVIEW, PARENT_TASK, AttemptKind, AttemptStatus, RunAttempt, UsageCompleteness
from tests.conftest import OPERATOR_TOKEN, PROTECTED_BASE_URL

REPO = "jan21deepak/droid_ai_engineer"
BASE = "0" * 40
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
BEARER = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "Origin": PROTECTED_BASE_URL}


def snapshot_attempts() -> list[tuple[str, int, str, str]]:
    with db_session() as session:
        return [
            (a.parent_type, a.parent_id, a.kind, a.status)
            for a in session.query(RunAttempt).order_by(RunAttempt.id.asc()).all()
        ]


def attempts_of(parent_type: str, parent_id: int) -> list[dict]:
    with db_session() as session:
        rows = (
            session.query(RunAttempt)
            .filter(RunAttempt.parent_type == parent_type, RunAttempt.parent_id == parent_id)
            .order_by(RunAttempt.sequence.asc())
            .all()
        )
        return [a.to_dict() for a in rows]


def turn(credits, *, success=True, session_id="sess-1", text="Done.\nBRANCH: droid/demo"):
    return {
        "session_id": session_id,
        "success": success,
        "subtype": "success" if success else "error_during_execution",
        "text": text if success else "",
        "duration_seconds": 3.0,
        "usage": {"input_tokens": 10, "output_tokens": 2, "factory_credits": credits},
        "error": None if success else "tool run failed",
    }


class FakeGitHub:
    def __init__(self, head=SHA_A):
        self.head = head
        self.calls: list[str] = []

    async def get_pull_request(self, repository, pr_number):
        self.calls.append("get_pull_request")
        return {"state": "open", "head": {"sha": self.head}, "base": {"ref": "main"}}

    async def post_issue_comment(self, repository, issue_number, body):
        self.calls.append("post_issue_comment")
        return True

    async def create_pull_request_review(self, *args, **kwargs):
        self.calls.append("create_pull_request_review")
        return {"id": 1}


class FakeDroid:
    """Stands in for DroidClient on every launch and continuation path; nothing reaches the SDK."""

    configured = True
    model = "auto"

    def __init__(self, heads=(SHA_A,)):
        self.heads = list(heads)
        self.turns: list[dict] = []
        self.inspected = 0

    @property
    def active_sessions(self):
        return set()

    def track(self, key, task):
        return task

    def session_busy(self, session_id):
        return False

    def _enter(self, kind, on_complete, **extra):
        self.turns.append({"kind": kind, "on_complete": on_complete, "entry": snapshot_attempts(), **extra})

    async def start_fix_run(self, **kwargs):
        self._enter("fix", kwargs["on_complete"], model=kwargs.get("model"))
        return {"session_id": "sess-1", "status": "running", "base_sha": BASE}

    async def send_follow_up(
        self, session_id, instruction, *, repository, cwd, model=None, on_complete=None, observer=None
    ):
        self._enter("follow_up", on_complete, instruction=instruction, cwd=cwd)
        return {
            "session_id": session_id,
            "status": "running",
            "base_sha": self.heads[min(self.inspected, len(self.heads)) - 1],
        }

    async def resume_interrupted(self, session_id, *, repository, cwd, model=None, on_complete=None, observer=None):
        self._enter("recovery", on_complete, cwd=cwd)
        return {"session_id": session_id, "status": "running", "base_sha": None}

    async def prepare_review_context(self, **kwargs):
        return ReviewContext(
            workspace=Path(f"/tmp/review-{kwargs['review_id']}"),
            base_ref=kwargs["base_ref"],
            base_sha=BASE,
            head_sha=SHA_A,
            diff="+change\n",
        )

    async def start_review_run(self, **kwargs):
        self._enter("review", kwargs["on_complete"])
        kwargs["on_session_opened"]("sess-review")
        return {"session_id": "sess-review", "status": "running"}

    async def inspect_workspace(self, repository, task_id, reported_branch):
        head = self.heads[min(self.inspected, len(self.heads) - 1)]
        self.inspected += 1
        return WorkspaceState(head_sha=head, branch=reported_branch, dirty=False)


@pytest.fixture
def droid(monkeypatch):
    monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", REPO)
    get_settings.cache_clear()
    fake = FakeDroid()
    monkeypatch.setattr(worker, "get_droid_client", lambda: fake)
    monkeypatch.setattr(recovery, "get_droid_client", lambda: fake)
    monkeypatch.setattr(worker, "GitHubClient", lambda: FakeGitHub())
    monkeypatch.setattr(reconcile, "GitHubClient", lambda: FakeGitHub())
    return fake


@pytest.fixture
def api(protected_client):
    protected_client.headers.update(BEARER)
    return protected_client


def add_task(**overrides) -> int:
    values = {
        "repository": REPO,
        "repository_url": f"https://github.com/{REPO}",
        "issue_number": 5,
        "issue_title": "Clarify docs",
        "status": TaskStatus.QUEUED,
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    with db_session() as session:
        task = Task(**values)
        session.add(task)
        session.flush()
        return task.id


async def launch(task_id: int) -> None:
    await worker.launch_fix_task(
        task_id, repository=REPO, repository_url="", issue_number=5, issue_title="Clarify docs", issue_body=""
    )


class TestImplementationAttempt:
    @pytest.mark.asyncio
    async def test_attempt_is_committed_before_spawn_and_duplicate_start_spawns_once(self, droid):
        task_id = add_task()

        await launch(task_id)
        await launch(task_id)

        assert len(droid.turns) == 1
        assert droid.turns[0]["entry"] == [(PARENT_TASK, task_id, AttemptKind.FIX, AttemptStatus.STARTING)]
        [attempt] = attempts_of(PARENT_TASK, task_id)
        assert (attempt["kind"], attempt["sequence"], attempt["status"]) == ("fix", 1, AttemptStatus.RUNNING)
        assert (attempt["session_id"], attempt["requested_model"], attempt["base_sha"]) == ("sess-1", "auto", BASE)
        # Facts not yet observed stay unknown.
        assert attempt["reported_model"] is None and attempt["head_sha"] is None
        assert attempt["started_at"] and attempt["completed_at"] is None
        assert attempt["usage"]["completeness"] == UsageCompleteness.UNKNOWN

    @pytest.mark.asyncio
    async def test_duplicate_terminal_callback_finalizes_and_counts_once(self, droid):
        task_id = add_task()
        await launch(task_id)
        on_complete = droid.turns[0]["on_complete"]

        await on_complete({**turn(10), "reported_model": "model-reported-by-sdk"})
        first = attempts_of(PARENT_TASK, task_id)
        await on_complete(turn(10))
        await on_complete(turn(99, success=False))

        assert attempts_of(PARENT_TASK, task_id) == first
        [attempt] = first
        assert (attempt["requested_model"], attempt["reported_model"]) == ("auto", "model-reported-by-sdk")
        assert (attempt["status"], attempt["head_sha"], attempt["usage"]["factory_credits"]) == ("completed", SHA_A, 10)
        assert attempt["usage"]["completeness"] == UsageCompleteness.COMPLETE
        with db_session() as session:
            task = session.get(Task, task_id)
            assert (task.status, task.factory_credits, task.estimated_tokens) == (TaskStatus.COMPLETED, 10, 12)
            # Agent success is only a local candidate: nothing published, nothing merged.
            assert (task.pull_request_url, task.pr_state, task.pr_merged_at) == (None, None, None)

    @pytest.mark.asyncio
    async def test_callback_finishing_after_start_cannot_be_reset_to_running(self, droid, monkeypatch):
        """A turn that completes before the launcher records "running" stays completed."""
        task_id = add_task()
        original = droid.start_fix_run

        async def finish_immediately(**kwargs):
            result = await original(**kwargs)
            await kwargs["on_complete"](turn(4))
            return result

        monkeypatch.setattr(droid, "start_fix_run", finish_immediately)
        await launch(task_id)

        [attempt] = attempts_of(PARENT_TASK, task_id)
        assert attempt["status"] == AttemptStatus.COMPLETED
        with db_session() as session:
            assert session.get(Task, task_id).status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_failed_start_closes_the_attempt_with_unknown_usage(self, droid, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("cannot open session")

        monkeypatch.setattr(droid, "start_fix_run", boom)
        task_id = add_task()
        await launch(task_id)

        [attempt] = attempts_of(PARENT_TASK, task_id)
        assert (attempt["status"], attempt["outcome_subtype"]) == (AttemptStatus.FAILED, "start_failed")
        assert attempt["usage"]["factory_credits"] is None
        with db_session() as session:
            assert session.get(Task, task_id).status == TaskStatus.FAILED


class TestReviewAttempt:
    @pytest.mark.asyncio
    async def test_review_attempt_is_committed_before_spawn_and_finalizes_once(self, droid):
        with db_session() as session:
            review = ReviewTask(repository=REPO, pr_number=3, status=TaskStatus.QUEUED)
            session.add(review)
            session.flush()
            review_id = review.id

        for _ in range(2):
            await worker.launch_review_task(review_id, repository=REPO, repository_url="", pr_number=3, pr_url="")

        assert len(droid.turns) == 1
        assert droid.turns[0]["entry"] == [(PARENT_REVIEW, review_id, AttemptKind.REVIEW, AttemptStatus.STARTING)]
        [attempt] = attempts_of(PARENT_REVIEW, review_id)
        assert (attempt["status"], attempt["session_id"]) == (AttemptStatus.RUNNING, "sess-review")
        assert (attempt["base_sha"], attempt["head_sha"]) == (BASE, SHA_A)

        review_text = 'Ok.\n```json\n{"verdict": "APPROVE", "summary": "s", "findings": []}\n```\nVERDICT: APPROVE'
        outcome = turn(1.5, session_id="sess-review", text=review_text)
        await droid.turns[0]["on_complete"](outcome)
        await droid.turns[0]["on_complete"](outcome)

        [attempt] = attempts_of(PARENT_REVIEW, review_id)
        assert (attempt["status"], attempt["usage"]["factory_credits"]) == (AttemptStatus.COMPLETED, 1.5)
        with db_session() as session:
            review = session.get(ReviewTask, review_id)
            assert (review.status, review.factory_credits, review.verdict) == ("completed", 1.5, "APPROVE")
            assert review.merged is False

    def test_review_detail_api_reports_attempts(self, api, droid):
        with db_session() as session:
            review = ReviewTask(repository=REPO, pr_number=3, status=TaskStatus.QUEUED)
            session.add(review)
            session.flush()
            review_id = review.id
        asyncio.run(worker.launch_review_task(review_id, repository=REPO, repository_url="", pr_number=3, pr_url=""))

        body = api.get(f"/api/reviews/{review_id}").json()
        assert body["review"]["id"] == review_id
        assert [(a["kind"], a["status"]) for a in body["attempts"]] == [("review", "running")]
        assert api.get("/api/reviews/999").status_code == 404


class TestHttpContinuation:
    def test_follow_up_appends_attempt_and_repeated_start_spawns_once(self, api, droid):
        task_id = add_task()
        asyncio.run(launch(task_id))
        asyncio.run(droid.turns[0]["on_complete"](turn(100)))

        first = api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "add a test"})
        second = api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "add a test"})

        assert first.status_code == 200, first.text
        assert (first.json()["sequence"], first.json()["kind"]) == (2, "follow_up")
        assert second.status_code == 409
        assert second.json()["active_attempt_id"] == first.json()["attempt_id"]
        assert [t["kind"] for t in droid.turns] == ["fix", "follow_up"]
        # SDK entry probe: the follow-up attempt was committed before the turn was spawned.
        assert droid.turns[1]["entry"][-1] == (PARENT_TASK, task_id, AttemptKind.FOLLOW_UP, AttemptStatus.STARTING)

        detail = api.get(f"/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == TaskStatus.RUNNING
        assert [(a["sequence"], a["kind"], a["status"]) for a in detail["attempts"]] == [
            (1, "fix", "completed"),
            (2, "follow_up", "running"),
        ]
        follow_up = detail["attempts"][1]
        assert (follow_up["session_id"], follow_up["base_sha"], follow_up["head_sha"]) == ("sess-1", SHA_A, None)
        assert follow_up["requested_model"] is None and follow_up["reported_model"] is None
        assert detail["accounting"]["attempt_factory_credits"] == 100

        asyncio.run(droid.turns[1]["on_complete"](turn(20)))
        asyncio.run(droid.turns[1]["on_complete"](turn(20)))
        detail = api.get(f"/api/tasks/{task_id}").json()
        assert [a["usage"]["factory_credits"] for a in detail["attempts"]] == [100, 20]
        assert detail["accounting"]["attempt_factory_credits"] == 120
        assert api.get(f"/api/attempts/{follow_up['id']}").json()["attempt"]["status"] == "completed"
        assert len(api.get(f"/api/tasks/{task_id}/attempts").json()["attempts"]) == 2

    def test_follow_ups_and_explicit_recovery_preserve_earlier_attempts(self, api, droid):
        droid.heads = [SHA_A, SHA_B, SHA_C]
        task_id = add_task()
        asyncio.run(launch(task_id))
        asyncio.run(droid.turns[0]["on_complete"](turn(100)))

        assert api.post(f"/api/tasks/{task_id}/recover").status_code == 409  # completed: use a follow-up
        assert api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "one"}).status_code == 200
        asyncio.run(droid.turns[1]["on_complete"](turn(20)))
        assert api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "two"}).status_code == 200
        asyncio.run(droid.turns[2]["on_complete"](turn(5, success=False)))
        recovered = api.post(f"/api/tasks/{task_id}/recover")
        assert recovered.status_code == 200, recovered.text
        asyncio.run(droid.turns[3]["on_complete"](turn(7)))

        detail = api.get(f"/api/tasks/{task_id}").json()
        attempts = detail["attempts"]
        assert [(a["sequence"], a["kind"], a["status"]) for a in attempts] == [
            (1, "fix", "completed"),
            (2, "follow_up", "completed"),
            (3, "follow_up", "failed"),
            (4, "recovery", "completed"),
        ]
        assert len({a["id"] for a in attempts}) == 4
        assert [a["usage"]["factory_credits"] for a in attempts] == [100, 20, 5, 7]
        assert {a["session_id"] for a in attempts} == {"sess-1"}
        assert attempts[3]["continued_from_attempt_id"] == attempts[2]["id"]
        assert "tool run failed" in attempts[2]["error"]
        accounting = detail["accounting"]
        assert accounting["attempt_factory_credits"] == 132
        assert detail["task"]["factory_credits"] == 132
        # The failed follow-up's spend stays visible and the retry is counted as its own attempt.
        assert accounting["attempt_status_counts"] == {"completed": 3, "failed": 1}
        assert accounting["attempt_kind_counts"] == {"fix": 1, "follow_up": 2, "recovery": 1}
        assert accounting["unsuccessful_attempt_factory_credits"] == 5
        assert accounting["coverage"] == UsageCompleteness.COMPLETE
        assert [a["head_sha"] for a in attempts] == [SHA_A, SHA_B, None, SHA_C]

        with db_session() as session:
            task = session.get(Task, task_id)
            # Evidence bound to an earlier candidate does not authorize the current one.
            assert not evidence_applies_to_candidate(task, SHA_A)
            assert not evidence_applies_to_candidate(task, SHA_B)
            assert evidence_applies_to_candidate(task, SHA_C)

    def test_follow_up_that_cannot_start_restores_the_task(self, api, droid, monkeypatch):
        task_id = add_task()
        asyncio.run(launch(task_id))
        asyncio.run(droid.turns[0]["on_complete"](turn(3)))

        async def broken(*args, **kwargs):
            raise RuntimeError("workspace is missing")

        monkeypatch.setattr(droid, "send_follow_up", broken)
        resp = api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "more"})

        assert resp.status_code == 502
        detail = api.get(f"/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == TaskStatus.COMPLETED
        assert [(a["kind"], a["status"]) for a in detail["attempts"]] == [("fix", "completed"), ("follow_up", "failed")]
        assert detail["attempts"][1]["usage"]["factory_credits"] is None

    def test_unknown_task_and_attempt(self, api, droid):
        assert api.get("/api/tasks/404").status_code == 404
        assert api.get("/api/tasks/404/attempts").status_code == 404
        assert api.get("/api/attempts/404").status_code == 404
        assert api.post("/api/tasks/404/recover").status_code == 404


def add_attempt(parent_type, parent_id, *, kind, status, sequence=1, session_id=None, **usage) -> int:
    with db_session() as session:
        attempt = RunAttempt(
            parent_type=parent_type,
            parent_id=parent_id,
            kind=kind,
            sequence=sequence,
            status=status,
            session_id=session_id,
            **usage,
        )
        session.add(attempt)
        session.flush()
        return attempt.id


class SpawnRecorder:
    def __init__(self, monkeypatch):
        self.spawns: list[str] = []
        for name in ("start_fix_run", "start_review_run", "send_follow_up"):
            monkeypatch.setattr(DroidClient, name, self._refuse(name))

        async def resume(client, session_id, *, repository, cwd, model=None, on_complete=None, observer=None):
            self.spawns.append(f"resume_interrupted:{session_id}")
            return {"session_id": session_id, "status": "running", "base_sha": None}

        monkeypatch.setattr(DroidClient, "resume_interrupted", resume)
        monkeypatch.setattr(DroidClient, "configured", property(lambda self: True))

    def _refuse(self, name):
        async def spawn(*args, **kwargs):
            self.spawns.append(name)
            raise AssertionError(f"unexpected SDK spawn: {name}")

        return spawn


class TestRestart:
    def test_restart_interrupts_orphans_and_only_explicit_recovery_spawns(self, protected_mode, monkeypatch):
        monkeypatch.setenv("FORGE_ALLOWED_REPOSITORIES", REPO)
        get_settings.cache_clear()
        recorder = SpawnRecorder(monkeypatch)
        monkeypatch.setattr(reconcile, "GitHubClient", lambda: FakeGitHub())

        impl = add_task(status=TaskStatus.RUNNING, droid_session_id="sess-impl")
        impl_attempt = add_attempt(
            PARENT_TASK,
            impl,
            kind=AttemptKind.FIX,
            status=AttemptStatus.RUNNING,
            session_id="sess-impl",
            factory_credits=4.0,
        )
        with db_session() as session:
            review = ReviewTask(
                repository=REPO,
                pr_number=8,
                status=TaskStatus.RUNNING,
                droid_session_id="sess-rev",
                commit_sha=SHA_A,
                base_sha=BASE,
                diff_complete=True,
                review_policy=REVIEW_POLICY_RESTRICTED,
            )
            session.add(review)
            session.flush()
            review_id = review.id
        review_attempt = add_attempt(
            PARENT_REVIEW, review_id, kind=AttemptKind.REVIEW, status=AttemptStatus.RUNNING, session_id="sess-rev"
        )
        stopped = add_attempt(
            PARENT_TASK,
            add_task(issue_number=6, status=TaskStatus.FAILED, droid_session_id="sess-stop"),
            kind=AttemptKind.FIX,
            status=AttemptStatus.STOPPED,
            session_id="sess-stop",
            factory_credits=2.0,
            usage_completeness=UsageCompleteness.PARTIAL,
        )
        blocked = add_attempt(
            PARENT_TASK,
            add_task(issue_number=7, status=TaskStatus.FAILED, droid_session_id="sess-block"),
            kind=AttemptKind.FOLLOW_UP,
            status=AttemptStatus.BLOCKED,
            session_id="sess-block",
        )
        completed = add_attempt(
            PARENT_TASK,
            add_task(issue_number=9, status=TaskStatus.COMPLETED, droid_session_id="sess-done"),
            kind=AttemptKind.FIX,
            status=AttemptStatus.COMPLETED,
            session_id="sess-done",
            factory_credits=9.0,
            usage_completeness=UsageCompleteness.COMPLETE,
        )
        five = [impl_attempt, review_attempt, stopped, blocked, completed]

        def rows():
            with db_session() as session:
                return {aid: session.get(RunAttempt, aid).to_dict() for aid in five}

        before = rows()
        from app.app import app

        with TestClient(app, base_url=PROTECTED_BASE_URL):
            pass
        after = rows()

        assert recorder.spawns == []
        assert after[impl_attempt]["status"] == AttemptStatus.INTERRUPTED
        assert after[impl_attempt]["usage"]["factory_credits"] == 4.0
        assert after[impl_attempt]["usage"]["completeness"] == UsageCompleteness.PARTIAL
        assert after[review_attempt]["status"] == AttemptStatus.INTERRUPTED
        assert after[review_attempt]["usage"]["completeness"] == UsageCompleteness.UNKNOWN
        assert after[review_attempt]["usage"]["factory_credits"] is None
        for untouched in (stopped, blocked, completed):
            assert after[untouched] == before[untouched]
        with db_session() as session:
            task = session.get(Task, impl)
            assert (task.status, task.droid_session_id) == (TaskStatus.INTERRUPTED, "sess-impl")
            assert session.get(ReviewTask, review_id).status == TaskStatus.FAILED

        # A second restart changes nothing and still spawns nothing.
        with TestClient(app, base_url=PROTECTED_BASE_URL) as api:
            assert rows() == after
            assert recorder.spawns == []
            api.headers.update(BEARER)
            listed = api.get(f"/api/tasks/{impl}").json()
            assert [a["status"] for a in listed["attempts"]] == ["interrupted"]
            assert listed["accounting"]["coverage"] == UsageCompleteness.PARTIAL

            resp = api.post(f"/api/tasks/{impl}/recover")
            assert resp.status_code == 200, resp.text
            assert recorder.spawns == ["resume_interrupted:sess-impl"]
            detail = api.get(f"/api/tasks/{impl}").json()

        assert [(a["kind"], a["status"]) for a in detail["attempts"]] == [
            ("fix", "interrupted"),
            ("recovery", "running"),
        ]
        assert detail["attempts"][1]["continued_from_attempt_id"] == impl_attempt
        assert detail["attempts"][0]["usage"]["factory_credits"] == 4.0
        assert detail["attempts"][1]["usage"]["factory_credits"] is None
        with db_session() as session:
            accounting = parent_accounting(session, PARENT_TASK, session.get(Task, impl))
        assert accounting["attempt_factory_credits"] == 4.0
        assert accounting["coverage"] == UsageCompleteness.PARTIAL
