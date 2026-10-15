"""Durable stop and conservative run limits (stop intent, one paid run, runtime, continuations, spending).

The SDK and GitHub are deterministic local fakes. ``FakeDroid`` records every
turn that would have been spawned and every interruption request, so "no paid
turn started" is observed directly rather than inferred.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from droid_sdk import TokenUsageUpdate, ToolCall

from app import droid_client, reconcile, recovery, run_control, tasks, worker
from app.attempts import fail_attempt_start, start_attempt
from app.candidates import WorkspaceState
from app.config import get_settings
from app.database import db_session
from app.droid_client import DroidClient
from app.models import ReviewTask, Task, TaskStatus
from app.review_policy import ReviewContext
from app.run_events import TurnRecorder
from app.run_models import PARENT_REVIEW, PARENT_TASK, AttemptKind, AttemptStatus, RunAttempt, UsageCompleteness
from app.tasks import create_and_dispatch_task
from tests.conftest import OPERATOR_TOKEN, PROTECTED_BASE_URL

REPO = "jan21deepak/droid_ai_engineer"
BASE = "0" * 40
SHA_A = "a" * 40
BEARER = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "Origin": PROTECTED_BASE_URL}
REVIEW_TEXT = 'Ok.\n```json\n{"verdict": "APPROVE", "summary": "s", "findings": []}\n```\nVERDICT: APPROVE'


def turn(credits, *, success=True, session_id="sess-1", text="Done.\nBRANCH: droid/demo"):
    return {
        "session_id": session_id,
        "success": success,
        "subtype": "success" if success else "interrupted",
        "text": text if success else "",
        "duration_seconds": 3.0,
        "usage": {"input_tokens": 10, "output_tokens": 2, "factory_credits": credits},
        "error": None,
    }


GITHUB_CALLS: list[str] = []


class FakeGitHub:
    calls = GITHUB_CALLS

    async def get_pull_request(self, repository, pr_number):
        FakeGitHub.calls.append("get_pull_request")
        return {"state": "open", "head": {"sha": SHA_A}, "base": {"ref": "main"}}

    async def post_issue_comment(self, repository, issue_number, body):
        FakeGitHub.calls.append("post_issue_comment")
        return True

    async def create_pull_request_review(self, *args, **kwargs):
        FakeGitHub.calls.append("create_pull_request_review")
        return {"id": 1}


class FakeDroid:
    """Stands in for DroidClient on launch, continuation and stop paths; nothing reaches the SDK."""

    configured = True
    model = "auto"

    def __init__(self):
        self.turns: list[dict] = []
        self.interrupts: list[str | None] = []
        self.live: set[str] = set()
        self.inspected = 0

    @property
    def active_sessions(self):
        return set(self.live)

    def track(self, key, task):
        return task

    def session_busy(self, session_id):
        return session_id in self.live

    async def interrupt_run(self, session_id):
        self.interrupts.append(session_id)
        return {
            "live_turn": session_id in self.live,
            "interrupt_sent": session_id in self.live,
            "interrupt_error": None,
        }

    async def start_fix_run(self, **kwargs):
        self.turns.append({"kind": "fix", "on_complete": kwargs["on_complete"], "observer": kwargs.get("observer")})
        return {"session_id": "sess-1", "status": "running", "base_sha": BASE}

    async def send_follow_up(
        self, session_id, instruction, *, repository, cwd, model=None, on_complete=None, observer=None
    ):
        self.turns.append({"kind": "follow_up", "on_complete": on_complete, "observer": observer})
        return {"session_id": session_id, "status": "running", "base_sha": SHA_A}

    async def resume_interrupted(self, session_id, *, repository, cwd, model=None, on_complete=None, observer=None):
        self.turns.append({"kind": "recovery", "on_complete": on_complete, "observer": observer})
        return {"session_id": session_id, "status": "running", "base_sha": SHA_A}

    async def prepare_review_context(self, **kwargs):
        return ReviewContext(
            workspace=Path(f"/tmp/review-{kwargs['review_id']}"),
            base_ref=kwargs["base_ref"],
            base_sha=BASE,
            head_sha=SHA_A,
            diff="+change\n",
        )

    async def start_review_run(self, **kwargs):
        self.turns.append({"kind": "review", "on_complete": kwargs["on_complete"], "observer": kwargs.get("observer")})
        kwargs["on_session_opened"]("sess-review")
        return {"session_id": "sess-review", "status": "running"}

    async def inspect_workspace(self, repository, task_id, reported_branch):
        self.inspected += 1
        return WorkspaceState(head_sha=SHA_A, branch=reported_branch, dirty=False)


def configure(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, str(value))
    get_settings.cache_clear()


@pytest.fixture
def droid(monkeypatch):
    configure(monkeypatch, FORGE_ALLOWED_REPOSITORIES=REPO)
    fake = FakeDroid()
    GITHUB_CALLS.clear()
    for module in (worker, recovery, run_control, droid_client, tasks):
        monkeypatch.setattr(module, "get_droid_client", lambda: fake)
    monkeypatch.setattr(worker, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(reconcile, "GitHubClient", FakeGitHub)
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


def add_review(**overrides) -> int:
    values = {"repository": REPO, "pr_number": 3, "status": TaskStatus.QUEUED}
    values.update(overrides)
    with db_session() as session:
        review = ReviewTask(**values)
        session.add(review)
        session.flush()
        return review.id


async def launch(task_id: int) -> None:
    await worker.launch_fix_task(
        task_id, repository=REPO, repository_url="", issue_number=5, issue_title="Clarify docs", issue_body=""
    )


async def launch_review(review_id: int) -> None:
    await worker.launch_review_task(review_id, repository=REPO, repository_url="", pr_number=3, pr_url="")


def attempts_of(parent_type: str, parent_id: int) -> list[dict]:
    with db_session() as session:
        rows = (
            session.query(RunAttempt)
            .filter(RunAttempt.parent_type == parent_type, RunAttempt.parent_id == parent_id)
            .order_by(RunAttempt.sequence.asc())
            .all()
        )
        return [a.to_dict() for a in rows]


def status_of(model, row_id) -> str:
    with db_session() as session:
        return session.get(model, row_id).status


def completed_task(droid) -> int:
    """A task whose implementation turn completed, ready for a follow-up."""
    task_id = add_task()
    asyncio.run(launch(task_id))
    asyncio.run(droid.turns[-1]["on_complete"](turn(100)))
    assert status_of(Task, task_id) == TaskStatus.COMPLETED
    return task_id


# ---------------------------------------------------------------------------
# VAL-RUN-003: durable stop intent wins finalization races
# ---------------------------------------------------------------------------


class TestDurableStop:
    def test_stopped_implementation_ignores_late_success_and_retains_usage_once(self, api, droid):
        task_id = add_task()
        asyncio.run(launch(task_id))
        droid.live.add("sess-1")

        stop = api.post(f"/api/tasks/{task_id}/stop")
        assert stop.status_code == 200, stop.text
        body = stop.json()
        assert (body["status"], body["stop_reason"], body["already_stopped"]) == ("stopped", "operator", False)
        assert body["interrupt"]["interrupt_sent"] is True
        assert droid.interrupts == ["sess-1"]
        [attempt] = attempts_of(PARENT_TASK, task_id)
        assert (attempt["status"], attempt["stop_reason"]) == (AttemptStatus.STOPPED, "operator")
        assert attempt["stop_requested_at"] and attempt["completed_at"]
        assert attempt["usage"]["completeness"] == UsageCompleteness.UNKNOWN

        # Repeated stop is harmless and requests nothing new.
        again = api.post(f"/api/tasks/{task_id}/stop")
        assert again.status_code == 200 and again.json()["already_stopped"] is True
        assert droid.interrupts == ["sess-1"]

        # The SDK's late terminal result (even a success) cannot revive or relabel the attempt.
        late = droid.turns[0]["on_complete"]
        droid.live.clear()
        asyncio.run(late(turn(42)))
        asyncio.run(late(turn(42)))

        [attempt] = attempts_of(PARENT_TASK, task_id)
        assert attempt["status"] == AttemptStatus.STOPPED
        assert (attempt["usage"]["factory_credits"], attempt["usage"]["completeness"]) == (42, "complete")
        with db_session() as session:
            task = session.get(Task, task_id)
            assert (task.status, task.factory_credits, task.candidate_sha) == (TaskStatus.STOPPED, 42, None)
        assert droid.inspected == 0 and FakeGitHub.calls == []
        events = [e["type"] for e in api.get(f"/api/attempts/{attempt['id']}").json()["events"]]
        assert events.count("stopped") == 1 and events.count("late_usage_retained") == 1
        assert "interrupt_requested" in events

    def test_stopped_review_ignores_late_verdict_and_posts_nothing(self, api, droid):
        review_id = add_review()
        asyncio.run(launch_review(review_id))
        assert status_of(ReviewTask, review_id) == TaskStatus.RUNNING

        assert api.post(f"/api/reviews/{review_id}/stop").status_code == 200
        assert droid.interrupts == ["sess-review"]
        asyncio.run(droid.turns[0]["on_complete"](turn(1.5, session_id="sess-review", text=REVIEW_TEXT)))

        [attempt] = attempts_of(PARENT_REVIEW, review_id)
        assert (attempt["status"], attempt["usage"]["factory_credits"]) == (AttemptStatus.STOPPED, 1.5)
        with db_session() as session:
            review = session.get(ReviewTask, review_id)
            assert (review.status, review.verdict, review.factory_credits) == (TaskStatus.STOPPED, None, 1.5)
        assert "create_pull_request_review" not in FakeGitHub.calls

    def test_stopped_follow_up_stays_stopped_and_restart_resumes_nothing(self, api, droid):
        task_id = completed_task(droid)
        assert api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "add a test"}).status_code == 200

        assert api.post(f"/api/tasks/{task_id}/stop").status_code == 200
        observer = droid.turns[-1]["observer"]
        # Delayed progress only adds timeline facts.
        observer.observe(ToolCall(tool_use_id="t1", name="Read", input={}))
        asyncio.run(droid.turns[-1]["on_complete"](turn(7)))

        asyncio.run(recovery.recover_interrupted_tasks(droid))
        turns_before = len(droid.turns)
        assert [(a["kind"], a["status"]) for a in attempts_of(PARENT_TASK, task_id)] == [
            ("fix", "completed"),
            ("follow_up", "stopped"),
        ]
        assert status_of(Task, task_id) == TaskStatus.STOPPED
        assert len(droid.turns) == turns_before

        # An explicit continuation is a new attempt under current policy; the stopped one is kept.
        resumed = api.post(f"/api/tasks/{task_id}/recover")
        assert resumed.status_code == 200, resumed.text
        assert [(a["kind"], a["status"]) for a in attempts_of(PARENT_TASK, task_id)][-1] == ("recovery", "running")
        assert attempts_of(PARENT_TASK, task_id)[1]["usage"]["factory_credits"] == 7

    def test_stop_while_starting_prevents_the_turn_and_survives_restart(self, droid, monkeypatch):
        task_id = add_task()
        original = droid.start_fix_run

        async def stop_during_workspace_prep(**kwargs):
            await run_control.stop_parent(PARENT_TASK, task_id)
            return await original(**kwargs)

        monkeypatch.setattr(droid, "start_fix_run", stop_during_workspace_prep)
        asyncio.run(launch(task_id))

        [attempt] = attempts_of(PARENT_TASK, task_id)
        assert attempt["status"] == AttemptStatus.STOPPED and attempt["started_at"] is None
        assert status_of(Task, task_id) == TaskStatus.STOPPED
        # The admission gate the real client checks before streaming refuses the stopped attempt.
        assert TurnRecorder(attempt["id"]).admitted() is False
        asyncio.run(recovery.recover_interrupted_tasks(droid))
        assert status_of(Task, task_id) == TaskStatus.STOPPED

    def test_stop_queued_review_before_launch_prevents_launch(self, api, droid):
        review_id = add_review()
        assert api.post(f"/api/reviews/{review_id}/stop").status_code == 200
        asyncio.run(launch_review(review_id))
        assert droid.turns == [] and attempts_of(PARENT_REVIEW, review_id) == []
        assert status_of(ReviewTask, review_id) == TaskStatus.STOPPED

    def test_stop_refuses_finished_or_missing_work(self, api, droid):
        task_id = completed_task(droid)
        assert api.post(f"/api/tasks/{task_id}/stop").status_code == 409
        assert api.post("/api/tasks/999/stop").status_code == 404
        assert api.post("/api/reviews/999/stop").status_code == 404

    def test_stop_requires_operator_access(self, protected_client, droid):
        task_id = add_task()
        assert protected_client.post(f"/api/tasks/{task_id}/stop").status_code in (401, 403)
        assert status_of(Task, task_id) == TaskStatus.QUEUED


class FakeSession:
    def __init__(self, session_id="sess-live"):
        self.id = session_id
        self.settings = SimpleNamespace(model="auto")
        self.streams = 0
        self.interrupted = 0
        self.closed = False
        self.release = asyncio.Event()

    async def interrupt(self):
        self.interrupted += 1

    async def close(self):
        self.closed = True

    def stream(self, prompt, **kwargs):
        self.streams += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.release.wait()
        raise StopAsyncIteration


class TestClientInterruption:
    @pytest.mark.asyncio
    async def test_stopped_attempt_is_never_streamed(self):
        task_id = add_task()
        attempt_id = start_attempt(PARENT_TASK, task_id, AttemptKind.FIX).attempt_id
        with db_session() as session:
            session.get(RunAttempt, attempt_id).status = AttemptStatus.STOPPED
        client = DroidClient(api_key="fk_test")
        session = FakeSession()
        completions = []

        async def on_complete(outcome):
            completions.append(outcome)

        client._spawn_turn(session, "prompt", kind="fix", on_complete=on_complete, observer=TurnRecorder(attempt_id))
        await asyncio.sleep(0)
        await asyncio.gather(*list(client._tasks.values()))
        assert (session.streams, session.closed, completions) == (0, True, [])

    @pytest.mark.asyncio
    async def test_interrupt_requests_sdk_interrupt_and_cancels_a_turn_that_does_not_wind_down(self):
        client = DroidClient(api_key="fk_test")
        client.stop_grace_seconds = 0.05
        session = FakeSession()
        client._spawn_turn(session, "prompt", kind="fix", on_complete=None)
        await asyncio.sleep(0)
        assert client.active_sessions == {"sess-live"}

        result = await client.interrupt_run("sess-live")
        assert result == {"live_turn": True, "interrupt_sent": True, "interrupt_error": None}
        assert session.interrupted == 1
        await asyncio.sleep(0.2)
        assert client.active_sessions == set() and session.closed
        # A late or repeated stop of a finished turn does nothing.
        assert (await client.interrupt_run("sess-live"))["live_turn"] is False
        assert (await client.interrupt_run(None))["live_turn"] is False


# ---------------------------------------------------------------------------
# VAL-RUN-009: sequential, bounded execution and configured spending checks
# ---------------------------------------------------------------------------


class TestOnePaidRun:
    def test_competing_implementation_review_and_follow_up_starts_are_refused(self, api, droid):
        done_id = completed_task(droid)
        running_id = add_task(issue_number=6)
        asyncio.run(launch(running_id))
        spawned = len(droid.turns)

        competing_id = add_task(issue_number=7)
        asyncio.run(launch(competing_id))
        review_id = add_review()
        asyncio.run(launch_review(review_id))
        follow_up = api.post(f"/api/tasks/{done_id}/follow-up", json={"instruction": "more"})

        assert len(droid.turns) == spawned
        assert follow_up.status_code == 409 and follow_up.json()["code"] == "run_capacity"
        assert follow_up.json()["blocked_attempt_id"] is None
        assert status_of(Task, done_id) == TaskStatus.COMPLETED
        for parent_type, parent_id, model in (
            (PARENT_TASK, competing_id, Task),
            (PARENT_REVIEW, review_id, ReviewTask),
        ):
            [blocked] = attempts_of(parent_type, parent_id)
            assert (blocked["status"], blocked["outcome_subtype"]) == (AttemptStatus.BLOCKED, "run_capacity")
            assert "FORGE_MAX_CONCURRENT_RUNS=1" in blocked["error"]
            assert blocked["usage"]["source"] == "not_started"
            assert status_of(model, parent_id) == TaskStatus.BLOCKED
        assert api.get("/api/run-limits").json()["runs_in_flight"] == 1

    def test_review_assignment_reports_the_block_without_launching(self, droid):
        add_task()
        asyncio.run(launch(1))
        result = asyncio.run(worker.start_pr_review(repository=REPO, pr_number=4))
        assert (result["started"], result["detail"]) == (False, "run_capacity")
        assert status_of(ReviewTask, result["review_id"]) == TaskStatus.BLOCKED
        assert "get_pull_request" not in FakeGitHub.calls

    def test_a_stopped_turn_still_winding_down_counts_as_in_flight(self, api, droid):
        task_id = add_task()
        asyncio.run(launch(task_id))
        droid.live.add("sess-1")
        api.post(f"/api/tasks/{task_id}/stop")

        other = add_task(issue_number=8)
        asyncio.run(launch(other))
        assert status_of(Task, other) == TaskStatus.BLOCKED
        droid.live.clear()
        third = add_task(issue_number=9)
        asyncio.run(launch(third))
        assert status_of(Task, third) == TaskStatus.RUNNING


class TestRuntimeLimit:
    def test_runtime_expiry_requests_interruption_with_a_fake_clock(self, monkeypatch, droid):
        configure(monkeypatch, DROID_TURN_TIMEOUT_SECONDS=10)
        task_id = add_task()
        asyncio.run(launch(task_id))
        [attempt] = attempts_of(PARENT_TASK, task_id)
        with db_session() as session:
            started = session.get(RunAttempt, attempt["id"]).started_at.replace(tzinfo=UTC)

        assert asyncio.run(run_control.enforce_runtime_limits(now=started + timedelta(seconds=9))) == []
        assert droid.interrupts == []
        assert asyncio.run(run_control.enforce_runtime_limits(now=started + timedelta(seconds=11))) == [attempt["id"]]

        assert droid.interrupts == ["sess-1"]
        [attempt] = attempts_of(PARENT_TASK, task_id)
        assert (attempt["status"], attempt["stop_reason"]) == (AttemptStatus.STOPPED, "runtime_limit")
        with db_session() as session:
            task = session.get(Task, task_id)
            assert task.status == TaskStatus.STOPPED and "runtime limit of 10s" in task.error
        # Already stopped: a later sweep does nothing.
        assert asyncio.run(run_control.enforce_runtime_limits(now=started + timedelta(seconds=60))) == []


class TestContinuationLimit:
    def test_extra_continuation_beyond_the_allowance_is_blocked(self, monkeypatch, api, droid):
        configure(monkeypatch, FORGE_MAX_CONTINUATIONS=1)
        task_id = completed_task(droid)
        assert api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "one"}).status_code == 200
        asyncio.run(droid.turns[-1]["on_complete"](turn(5)))
        spawned = len(droid.turns)

        refused = api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "two"})
        assert refused.status_code == 409
        assert (refused.json()["code"], refused.json()["blocked"]) == ("continuation_limit", True)
        assert "1 of 1" in refused.json()["detail"]
        assert api.post(f"/api/tasks/{task_id}/recover").status_code == 409
        assert len(droid.turns) == spawned
        assert [(a["kind"], a["status"]) for a in attempts_of(PARENT_TASK, task_id)] == [
            ("fix", "completed"),
            ("follow_up", "completed"),
            ("follow_up", "blocked"),
        ]
        # The earlier candidate and outcome are untouched by the refusal.
        assert status_of(Task, task_id) == TaskStatus.COMPLETED


class TestSpendingChecks:
    @pytest.mark.parametrize(
        ("env", "fragment"),
        [
            ({"FORGE_BUDGET_POLICY": "credit_limit"}, "requires FORGE_CREDIT_LIMIT"),
            ({"FORGE_BUDGET_POLICY": "account_cap", "FORGE_DEMO_ACCOUNT_CAP_USD": ""}, "FORGE_DEMO_ACCOUNT_CAP_USD"),
        ],
        ids=["credit_limit", "account_cap"],
    )
    def test_missing_spending_input_blocks_before_spawn_and_across_restart(self, monkeypatch, droid, env, fragment):
        configure(monkeypatch, **env)
        result = asyncio.run(
            create_and_dispatch_task(
                repository=REPO, repository_url="", issue_number=11, issue_title="t", issue_body=""
            )
        )
        task_id = result["task_id"]
        assert (result["status"], result["detail"]) == ("blocked", "spending_check_input_missing")
        assert fragment in result["reason"]
        asyncio.run(launch(task_id))
        assert droid.turns == []

        asyncio.run(recovery.recover_interrupted_tasks(droid))
        with db_session() as session:
            task = session.get(Task, task_id)
            assert task.status == TaskStatus.BLOCKED and fragment in task.error
        [blocked] = attempts_of(PARENT_TASK, task_id)
        assert (blocked["status"], blocked["outcome_subtype"]) == ("blocked", "spending_check_input_missing")

    def test_exhausted_credit_threshold_blocks_follow_up_and_review(self, monkeypatch, api, droid):
        task_id = completed_task(droid)
        configure(monkeypatch, FORGE_BUDGET_POLICY="credit_limit", FORGE_CREDIT_LIMIT="100")
        refused = api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "more"})
        assert refused.status_code == 409 and refused.json()["code"] == "spending_limit_reached"
        review_id = add_review()
        asyncio.run(launch_review(review_id))
        assert len(droid.turns) == 1
        assert status_of(ReviewTask, review_id) == TaskStatus.BLOCKED
        spending = api.get("/api/run-limits").json()["spending"]
        assert (spending["allowed"], spending["observed_factory_credits"], spending["credit_limit"]) == (
            False,
            100,
            100,
        )

    def test_credit_limit_fails_closed_on_unknown_usage(self, monkeypatch, droid):
        task_id = add_task()
        asyncio.run(launch(task_id))
        asyncio.run(droid.turns[0]["on_complete"]({**turn(None, success=False), "usage": None}))
        configure(monkeypatch, FORGE_BUDGET_POLICY="credit_limit", FORGE_CREDIT_LIMIT="1000")
        other = add_task(issue_number=12)
        asyncio.run(launch(other))
        [blocked] = attempts_of(PARENT_TASK, other)
        assert blocked["outcome_subtype"] == "spending_check_input_missing"
        assert "unknown usage" in blocked["error"]

    def test_credit_limit_refuses_tokens_only_terminal_usage_before_spawn_and_across_restart(
        self, monkeypatch, api, droid
    ):
        task_id = add_task()
        asyncio.run(launch(task_id))
        asyncio.run(droid.turns[0]["on_complete"](turn(None)))
        [finished] = attempts_of(PARENT_TASK, task_id)
        assert (finished["usage"]["completeness"], finished["usage"]["factory_credits"]) == ("complete", None)
        assert finished["usage"]["input_tokens"] == 10
        configure(monkeypatch, FORGE_BUDGET_POLICY="credit_limit", FORGE_CREDIT_LIMIT="100")

        spending = api.get("/api/run-limits").json()["spending"]
        assert (spending["allowed"], spending["code"]) == (False, "spending_check_input_missing")
        assert spending["attempts_missing_factory_credits"] == 1
        refused = api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "more"})
        assert refused.status_code == 409 and refused.json()["code"] == "spending_check_input_missing"
        assert len(droid.turns) == 1

        asyncio.run(recovery.recover_interrupted_tasks(droid))
        blocked = attempts_of(PARENT_TASK, task_id)[-1]
        assert (blocked["status"], blocked["outcome_subtype"]) == ("blocked", "spending_check_input_missing")
        assert "no observed Factory credits" in blocked["error"]
        again = api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "more"})
        assert again.status_code == 409 and again.json()["code"] == "spending_check_input_missing"
        assert len(droid.turns) == 1

    def test_credit_limit_refuses_tokens_only_partial_usage(self, monkeypatch, droid):
        task_id = add_task()
        asyncio.run(launch(task_id))
        attempt_id = attempts_of(PARENT_TASK, task_id)[0]["id"]
        recorder = TurnRecorder(attempt_id, snapshot_interval=0)
        recorder.observe(
            TokenUsageUpdate(
                input_tokens=40,
                output_tokens=3,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                thinking_tokens=0,
                factory_credits=None,
            )
        )
        asyncio.run(recovery.recover_interrupted_tasks(droid))
        [partial] = attempts_of(PARENT_TASK, task_id)
        assert (partial["usage"]["completeness"], partial["usage"]["factory_credits"]) == ("partial", None)
        configure(monkeypatch, FORGE_BUDGET_POLICY="credit_limit", FORGE_CREDIT_LIMIT="100")

        other = add_task(issue_number=12)
        asyncio.run(launch(other))
        assert len(droid.turns) == 1
        assert status_of(Task, other) == TaskStatus.BLOCKED
        [blocked] = attempts_of(PARENT_TASK, other)
        assert blocked["outcome_subtype"] == "spending_check_input_missing"

    def test_credit_limit_admits_known_zero_lower_bounds_and_unstarted_turns(self, monkeypatch, api, droid):
        zero = add_task()
        asyncio.run(launch(zero))
        asyncio.run(droid.turns[0]["on_complete"](turn(0)))
        lower_bound = add_task(issue_number=13)
        asyncio.run(launch(lower_bound))
        recorder = TurnRecorder(attempts_of(PARENT_TASK, lower_bound)[0]["id"], snapshot_interval=0)
        recorder.observe(
            TokenUsageUpdate(
                input_tokens=40,
                output_tokens=3,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                thinking_tokens=0,
                factory_credits=6,
            )
        )
        asyncio.run(recovery.recover_interrupted_tasks(droid))
        never_started = add_task(issue_number=14)
        attempt_id = start_attempt(PARENT_TASK, never_started, AttemptKind.FIX).attempt_id
        fail_attempt_start(attempt_id, "Droid session failed to start")
        configure(monkeypatch, FORGE_BUDGET_POLICY="unlimited")
        refused_launch = add_task(issue_number=15)
        asyncio.run(launch(refused_launch))
        assert attempts_of(PARENT_TASK, refused_launch)[0]["status"] == "blocked"
        configure(monkeypatch, FORGE_BUDGET_POLICY="credit_limit", FORGE_CREDIT_LIMIT="100")

        spending = api.get("/api/run-limits").json()["spending"]
        assert (spending["allowed"], spending["observed_factory_credits"]) == (True, 6)
        assert spending["attempts_missing_factory_credits"] == 0
        assert any("lower bound" in note for note in spending["notes"])
        follow_up = api.post(f"/api/tasks/{zero}/follow-up", json={"instruction": "more"})
        assert follow_up.status_code == 200, follow_up.text
        assert len(droid.turns) == 3

    def test_unknown_policy_fails_closed(self, monkeypatch, droid):
        configure(monkeypatch, FORGE_BUDGET_POLICY="unlimited")
        task_id = add_task()
        asyncio.run(launch(task_id))
        assert droid.turns == [] and status_of(Task, task_id) == TaskStatus.BLOCKED
        assert attempts_of(PARENT_TASK, task_id)[0]["outcome_subtype"] == "spending_policy_unknown"

    def test_account_cap_demo_policy_allows_unknown_usd_honestly(self, monkeypatch, api, droid):
        configure(
            monkeypatch,
            FORGE_BUDGET_POLICY="account_cap",
            FORGE_DEMO_ACCOUNT_CAP_USD="50",
            FORGE_USD_PER_CREDIT_ESTIMATE="",
            FORGE_USD_ESTIMATE_SOURCE="",
        )
        task_id = completed_task(droid)
        follow_up = api.post(f"/api/tasks/{task_id}/follow-up", json={"instruction": "more"})
        assert follow_up.status_code == 200, follow_up.text

        limits = api.get("/api/run-limits").json()
        assert (limits["max_concurrent_runs"], limits["runs_in_flight"]) == (1, 1)
        spending = limits["spending"]
        assert (spending["policy"], spending["allowed"], spending["enforced_by"]) == (
            "account_cap",
            True,
            "factory_account",
        )
        assert (spending["account_cap_usd"], spending["app_enforces_usd_ceiling"]) == (50, False)
        assert spending["observed_factory_credits"] == 100
        assert spending["usd"]["status"] == "unknown"
        assert spending["usd"]["estimated_usd"] is None and spending["usd"]["rate_usd_per_credit"] is None
        assert any("not by Forge" in note for note in spending["notes"])
        # Dollars stay unknown on the attempt too; nothing is converted.
        detail = api.get(f"/api/tasks/{task_id}").json()
        assert detail["accounting"]["usd"]["total_usd"] is None
        assert all(a["cost"]["status"] == "unknown" for a in detail["attempts"])

    def test_blocked_attempts_do_not_count_as_unknown_cost(self, monkeypatch, api, droid):
        configure(monkeypatch, FORGE_BUDGET_POLICY="unlimited")
        task_id = add_task()
        asyncio.run(launch(task_id))
        accounting = api.get(f"/api/tasks/{task_id}").json()["accounting"]
        assert accounting["attempts_with_unknown_usage"] == 0
        assert accounting["usd"]["attempts_unknown"] == 0
