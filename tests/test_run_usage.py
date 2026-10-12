"""Usage ledger, operational timeline and dollar accounting per attempt.

Turns run through the real ``DroidClient._run_turn`` with a scripted SDK
session that streams real ``droid_sdk`` event objects, so the timeline
recorder, snapshot handling and terminal extraction are all exercised. Nothing
reaches the SDK process or GitHub.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
from droid_sdk import (
    AssistantMessage,
    ErrorEvent,
    RunFailure,
    RunSuccess,
    TextBlock,
    ThinkingBlock,
    ThinkingDelta,
    TokenUsageUpdate,
    ToolCall,
    ToolResult,
    Usage,
)
from droid_sdk.errors import RunTimeoutError

from app import database, reconcile, recovery, worker
from app.attempts import fail_attempt_start, mark_attempt_running, parent_accounting, start_attempt
from app.config import get_settings
from app.database import db_session
from app.droid_client import DroidClient
from app.models import Task, TaskStatus
from app.run_events import MAX_EVENTS_PER_ATTEMPT, EventType, TurnRecorder
from app.run_models import PARENT_TASK, AttemptKind, AttemptStatus, CostStatus, RunAttempt, UsageCompleteness
from app.usage import clean_usage, finite_number, record_terminal_usage
from tests.test_run_attempts import BEARER, REPO, FakeDroid, FakeGitHub, add_task

THINKING_SENTINEL = "PRIVATE-THINKING-SENTINEL-7f3a"
FAKE_TOKEN = "fake-sentinel-credential-0123456789abcdef"
SHAPED_TOKEN = "ghp_" + "Z" * 36
CREDENTIAL_URL = f"https://x-access-token:{SHAPED_TOKEN}@github.com/{REPO}.git"


class ScriptedStream:
    """Yields the scripted events, then raises ``error`` or yields the terminal result (like the SDK)."""

    def __init__(self, messages, result, error):
        self._events = [*messages, *([result] if result is not None and error is None else [])]
        self._error = error
        self.result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        if self._error is not None:
            raise self._error
        raise StopAsyncIteration


class ScriptedSession:
    def __init__(self, session_id, messages=(), result=None, error=None):
        self.id = session_id
        self.settings = SimpleNamespace(model="model-under-test")
        self._script = (messages, result, error)
        self.stream_kwargs: dict = {}

    def stream(self, prompt, *, timeout=None, **kwargs):
        self.stream_kwargs = {"timeout": timeout, **kwargs}
        return ScriptedStream(*self._script)

    async def close(self):
        return None


def usage(credits, *, inp=0, out=0, cache_read=0, cache_creation=0, thinking=0):
    return Usage(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        thinking_tokens=thinking,
        factory_credits=credits,
    )


def snapshot(credits, **tokens):
    u = usage(credits, **tokens)
    return TokenUsageUpdate(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_creation_tokens=u.cache_creation_tokens,
        cache_read_tokens=u.cache_read_tokens,
        thinking_tokens=u.thinking_tokens,
        factory_credits=u.factory_credits,
    )


def success(session_id, terminal_usage, text="Done.\nBRANCH: droid/demo"):
    return RunSuccess(
        text=text,
        messages=(),
        usage=terminal_usage,
        duration=timedelta(seconds=3),
        turn_count=1,
        session_id=session_id,
    )


def failure(session_id, terminal_usage):
    return RunFailure(
        subtype="error_during_execution",
        text="",
        messages=(),
        usage=terminal_usage,
        duration=timedelta(seconds=2),
        turn_count=1,
        session_id=session_id,
    )


def assistant(*blocks):
    now = datetime.now(UTC)
    return AssistantMessage(id="m1", content=tuple(blocks), text="", created_at=now, updated_at=now)


@pytest.fixture
def fakes(monkeypatch):
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


def begin(task_id, kind, session_id="sess-1") -> int:
    attempt_id = start_attempt(PARENT_TASK, task_id, kind, parent_status=TaskStatus.RUNNING).attempt_id
    mark_attempt_running(attempt_id, session_id=session_id)
    with db_session() as session:
        session.get(Task, task_id).droid_session_id = session_id
    return attempt_id


def run_turn(task_id, attempt_id, session, *, kind="fix"):
    """One turn through the real client loop, observer and attempt-bound finalization."""
    on_complete = partial(worker.finalize_fix_task, task_id, attempt_id=attempt_id)
    recorder = TurnRecorder(attempt_id, snapshot_interval=0)
    outcome = asyncio.run(
        DroidClient()._run_turn(session, "prompt", kind=kind, on_complete=on_complete, observer=recorder)
    )
    return outcome, on_complete, recorder


def attempt_row(attempt_id) -> dict:
    with db_session() as session:
        return session.get(RunAttempt, attempt_id).to_dict()


def accounting(task_id) -> dict:
    with db_session() as session:
        return parent_accounting(session, PARENT_TASK, session.get(Task, task_id))


class TestNumbers:
    @pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), -1, True, "7", None, 10**16, [1]])
    def test_unsafe_values_are_unknown(self, value):
        assert finite_number(value, integer=True, maximum=10**15) is None

    def test_terminal_usage_rejects_infinite_and_negative_categories(self):
        attempt = RunAttempt(kind=AttemptKind.FIX)
        assert record_terminal_usage(
            attempt, {"factory_credits": float("inf"), "input_tokens": -5, "output_tokens": 12}
        )
        assert (attempt.factory_credits, attempt.input_tokens, attempt.output_tokens) == (None, None, 12)
        assert clean_usage({"factory_credits": float("nan")}) == {}


class TestSnapshotsAndTerminalTotals:
    def test_readiness_shaped_turns_aggregate_once(self, fakes):
        """Terminal/cumulative credits 3267/3267 then 50/3317 add up to 3317, not 6584."""
        task_id = add_task()
        first = begin(task_id, AttemptKind.FIX)
        session = ScriptedSession("sess-1", [snapshot(1200), snapshot(3267)], result=success("sess-1", usage(3267)))
        outcome, on_complete_1, _ = run_turn(task_id, first, session)
        assert session.stream_kwargs["include_partial_messages"] is True
        assert outcome["usage"]["factory_credits"] == 3267

        second = begin(task_id, AttemptKind.FOLLOW_UP)
        resumed = ScriptedSession("sess-1", [snapshot(3267), snapshot(3317)], result=success("sess-1", usage(50)))
        _, on_complete_2, recorder = run_turn(task_id, second, resumed, kind="follow_up")

        totals = accounting(task_id)
        assert [attempt_row(a)["usage"]["factory_credits"] for a in (first, second)] == [3267, 50]
        assert totals["attempt_factory_credits"] == 3317
        assert attempt_row(second)["usage"]["snapshot"]["latest"]["factory_credits"] == 3317
        assert attempt_row(second)["usage"]["source"] == "terminal_result"

        # Replayed snapshots and terminal callbacks change nothing.
        recorder.observe(snapshot(3317))
        recorder.observe(snapshot(3317))
        asyncio.run(on_complete_1({"success": True, "usage": {"factory_credits": 3267}}))
        asyncio.run(on_complete_2({"success": True, "usage": {"factory_credits": 3317}}))
        again = accounting(task_id)
        assert again["attempt_factory_credits"] == 3317
        assert again["coverage"] == UsageCompleteness.COMPLETE
        with db_session() as session:
            assert session.get(Task, task_id).factory_credits == 3317

    def test_categories_stay_separate_and_missing_ones_stay_unknown(self, fakes):
        task_id = add_task()
        full = begin(task_id, AttemptKind.FIX)
        terminal = usage(7, inp=100, out=20, cache_read=30, cache_creation=40, thinking=5)
        run_turn(task_id, full, ScriptedSession("sess-1", result=success("sess-1", terminal)))
        sparse = begin(task_id, AttemptKind.FOLLOW_UP)
        only_credits = SimpleNamespace(
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            thinking_tokens=None,
            factory_credits=3,
        )
        sparse_result = SimpleNamespace(
            success=True, subtype="success", text="ok", duration=timedelta(seconds=1), usage=only_credits
        )
        run_turn(task_id, sparse, ScriptedSession("sess-1", result=sparse_result), kind="follow_up")

        first = attempt_row(full)["usage"]
        assert {k: first[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens")} == {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 30,
        }
        assert (first["cache_creation_tokens"], first["thinking_tokens"], first["factory_credits"]) == (40, 5, 7)
        second = attempt_row(sparse)["usage"]
        assert second["factory_credits"] == 3
        assert all(second[k] is None for k in ("input_tokens", "output_tokens", "thinking_tokens"))

        totals = accounting(task_id)["usage_totals"]
        assert totals["input_tokens"] == 100 and totals["cache_creation_tokens"] == 40
        assert totals["factory_credits"] == 10
        assert totals["attempts_observed"]["input_tokens"] == 1
        assert totals["attempts_observed"]["factory_credits"] == 2
        # No invented billable total from adding cache/thinking categories.
        assert not any("billable" in key or key == "total_tokens" for key in totals)
        assert attempt_row(full)["cost"]["status"] == CostStatus.UNKNOWN


class TestFailedRetriedAndTimedOutWork:
    def test_failures_timeouts_and_retries_stay_in_usage(self, fakes):
        task_id = add_task()
        ok = begin(task_id, AttemptKind.FIX)
        run_turn(task_id, ok, ScriptedSession("sess-1", [snapshot(10)], result=success("sess-1", usage(10))))
        failed = begin(task_id, AttemptKind.FOLLOW_UP)
        run_turn(task_id, failed, ScriptedSession("sess-1", result=failure("sess-1", usage(3))), kind="follow_up")
        timed_out = begin(task_id, AttemptKind.FOLLOW_UP)
        timeout = RunTimeoutError("The run exceeded its timeout", timeout_duration=600)
        outcome, _, _ = run_turn(
            task_id,
            timed_out,
            ScriptedSession("sess-1", [snapshot(13), snapshot(17)], error=timeout),
            kind="follow_up",
        )
        assert (outcome["subtype"], outcome["success"]) == ("timeout", False)
        retry = begin(task_id, AttemptKind.RECOVERY)
        run_turn(task_id, retry, ScriptedSession("sess-1", result=success("sess-1", usage(2))), kind="recovery")

        rows = [attempt_row(a) for a in (ok, failed, timed_out, retry)]
        assert [r["status"] for r in rows] == ["completed", "failed", "failed", "completed"]
        assert [r["usage"]["factory_credits"] for r in rows] == [10, 3, 4, 2]
        assert rows[2]["usage"]["completeness"] == UsageCompleteness.PARTIAL
        assert rows[2]["usage"]["source"] == "snapshot_delta_lower_bound"
        assert rows[2]["outcome_subtype"] == "timeout"
        totals = accounting(task_id)
        assert totals["attempt_factory_credits"] == 19
        assert totals["coverage"] == UsageCompleteness.PARTIAL
        assert totals["unsuccessful_attempt_factory_credits"] == 7
        assert totals["attempts_with_partial_usage"] == 1
        assert totals["attempt_kind_counts"]["recovery"] == 1
        assert totals["attribution_limits"]

    def test_failure_before_any_observation_is_unknown_not_zero(self, fakes):
        task_id = add_task()
        attempt_id = start_attempt(PARENT_TASK, task_id, AttemptKind.FIX).attempt_id
        fail_attempt_start(attempt_id, "Droid session failed to start")

        row = attempt_row(attempt_id)
        assert row["usage"]["factory_credits"] is None
        assert row["usage"]["completeness"] == UsageCompleteness.UNKNOWN
        totals = accounting(task_id)
        assert totals["attempt_factory_credits"] is None and totals["coverage"] == UsageCompleteness.UNKNOWN

    def test_ambiguous_resumed_snapshot_is_not_a_charge(self, fakes):
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FOLLOW_UP)
        timeout = RunTimeoutError("timeout", timeout_duration=1)
        run_turn(task_id, attempt_id, ScriptedSession("sess-1", [snapshot(500)], error=timeout), kind="follow_up")

        row = attempt_row(attempt_id)
        assert row["usage"]["factory_credits"] is None
        assert row["usage"]["completeness"] == UsageCompleteness.UNKNOWN
        assert row["usage"]["snapshot"]["latest"]["factory_credits"] == 500

    def test_restart_keeps_persisted_snapshot_of_a_fresh_session_as_partial(self, fakes):
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FIX, session_id="sess-crash")
        recorder = TurnRecorder(attempt_id, snapshot_interval=0)
        recorder.started("fix")
        recorder.observe(snapshot(6, inp=50, out=9))

        assert asyncio.run(recovery.recover_interrupted_tasks(droid=fakes)) == 1

        row = attempt_row(attempt_id)
        assert row["status"] == AttemptStatus.INTERRUPTED
        assert (row["usage"]["factory_credits"], row["usage"]["input_tokens"]) == (6, 50)
        assert row["usage"]["completeness"] == UsageCompleteness.PARTIAL
        assert row["usage"]["source"] == "snapshot_fresh_session"
        with db_session() as session:
            assert session.get(Task, task_id).factory_credits == 6


def noisy_turn_messages():
    big = "x" * 20_000
    return [
        assistant(TextBlock(text=f"Starting. token={FAKE_TOKEN} {big}"), ThinkingBlock(thinking=THINKING_SENTINEL, signature="s")),
        ThinkingDelta(message_id="m1", block_index=1, text=THINKING_SENTINEL),
        ToolCall(name="Execute", tool_use_id="t1", input={"command": f"git push {CREDENTIAL_URL}"}),
        ToolResult(tool_use_id="t1", tool_name="Execute", content=f"fatal: could not read {CREDENTIAL_URL} {big}", is_error=True),
        snapshot(2, inp=10, out=3),
        ErrorEvent(message=f"transport failed with GITHUB_TOKEN={FAKE_TOKEN}", error_type="Error"),
    ]  # fmt: skip


class TestTimeline:
    def test_timeline_is_ordered_bounded_and_scrubbed(self, fakes, api, monkeypatch, caplog):
        monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
        get_settings.cache_clear()
        caplog.set_level(logging.DEBUG)
        task_id = add_task()
        ok = begin(task_id, AttemptKind.FIX)
        run_turn(task_id, ok, ScriptedSession("sess-1", noisy_turn_messages(), result=success("sess-1", usage(2))))
        bad = begin(task_id, AttemptKind.FOLLOW_UP)
        run_turn(
            task_id,
            bad,
            ScriptedSession("sess-1", noisy_turn_messages(), result=failure("sess-1", usage(1))),
            kind="follow_up",
        )

        # Survives reopening the database.
        database.reset_for_tests()
        body = api.get(f"/api/attempts/{ok}").json()
        types = [e["type"] for e in body["events"]]
        assert types == [
            EventType.TURN_STARTED,
            EventType.ASSISTANT_MESSAGE,
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
            EventType.USAGE,
            EventType.ERROR,
            EventType.TURN_FINISHED,
        ]
        assert [e["sequence"] for e in body["events"]] == list(range(1, 8))
        tool_result = body["events"][3]["payload"]
        assert tool_result["tool"] == "Execute" and tool_result["is_error"] is True
        assert "could not read" in tool_result["error"]
        assert body["events"][2]["payload"] == {"tool": "Execute"}
        assert body["events"][4]["payload"]["cumulative"]["factory_credits"] == 2
        assert body["events"][-1]["payload"]["success"] is True

        timeline = api.get(f"/api/tasks/{task_id}/timeline").json()
        assert [(e["attempt_sequence"], e["sequence"]) for e in timeline["events"]] == sorted(
            (e["attempt_sequence"], e["sequence"]) for e in timeline["events"]
        )
        finished = [e for e in timeline["events"] if e["type"] == EventType.TURN_FINISHED]
        assert [(e["kind"], e["payload"]["success"]) for e in finished] == [("fix", True), ("follow_up", False)]

        dumped = json.dumps([body, timeline])
        for secret in (FAKE_TOKEN, SHAPED_TOKEN, THINKING_SENTINEL):
            assert secret not in dumped
            assert secret not in caplog.text
        assert all(len(json.dumps(e["payload"])) <= 2000 for e in timeline["events"])
        assert attempt_row(ok)["usage"]["thinking_tokens"] == 0

    def test_event_count_is_capped_but_terminal_event_is_kept(self, fakes):
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FIX)
        calls = [ToolCall(name="Read", tool_use_id=f"t{i}", input={}) for i in range(400)]
        run_turn(task_id, attempt_id, ScriptedSession("sess-1", calls, result=success("sess-1", usage(1))))

        with db_session() as session:
            from app.run_events import list_events

            events = [e.to_dict() for e in list_events(session, [attempt_id])]
        assert len(events) <= MAX_EVENTS_PER_ATTEMPT
        assert events[-1]["type"] == EventType.TURN_FINISHED
        assert events[-1]["payload"]["events_dropped"] > 0


def finished_attempt(task_id, credits, kind=AttemptKind.FIX) -> int:
    attempt_id = begin(task_id, kind)
    run_turn(task_id, attempt_id, ScriptedSession("sess-1", result=success("sess-1", usage(credits))), kind=kind)
    return attempt_id


class TestDollars:
    def test_unknown_estimated_and_reconciled_stay_distinct(self, fakes, api):
        task_id = add_task()
        a = finished_attempt(task_id, 100)
        b = finished_attempt(task_id, 100, AttemptKind.FOLLOW_UP)
        c = finished_attempt(task_id, 100, AttemptKind.FOLLOW_UP)

        alone = api.get(f"/api/tasks/{task_id}").json()["accounting"]["usd"]
        assert (alone["coverage"], alone["known_usd_subtotal"], alone["total_usd"]) == ("unknown", None, None)

        rate = {"rate_usd_per_credit": 0.001, "source": "test-only fixture rate"}
        resp = api.post(f"/api/attempts/{b}/cost-estimate", json=rate)
        assert resp.status_code == 200, resp.text
        assert resp.json()["cost"]["estimated_usd"] == pytest.approx(0.10)
        assert api.post(f"/api/attempts/{c}/cost-estimate", json=rate).status_code == 200
        manual = {"usd": 0.12, "source": "manual invoice fixture"}
        assert api.post(f"/api/attempts/{c}/cost-reconciliation", json=manual).status_code == 200

        detail = api.get(f"/api/tasks/{task_id}").json()
        costs = {row["id"]: row["cost"] for row in detail["attempts"]}
        assert costs[a]["status"] == CostStatus.UNKNOWN and costs[a]["estimated_usd"] is None
        assert (costs[b]["status"], costs[b]["estimate_source"]) == (CostStatus.ESTIMATED, "test-only fixture rate")
        assert costs[b]["billed_usd"] is None and costs[b]["reconciled_usd"] is None
        assert costs[c]["status"] == CostStatus.RECONCILED
        assert costs[c]["estimated_usd"] == pytest.approx(0.10) and costs[c]["reconciled_usd"] == 0.12
        usd = detail["accounting"]["usd"]
        assert usd["coverage"] == UsageCompleteness.PARTIAL
        assert (usd["attempts_unknown"], usd["attempts_estimated"], usd["attempts_reconciled"]) == (1, 1, 1)
        assert usd["reconciled_usd_subtotal"] == 0.12
        assert usd["estimated_usd_subtotal"] == pytest.approx(0.10)
        assert usd["estimates_superseded_by_reconciliation"] == 1
        assert usd["known_usd_subtotal"] == pytest.approx(0.22)
        assert usd["total_usd"] is None and usd["billed_usd"] is None
        assert detail["accounting"]["attempt_factory_credits"] == 300

    @pytest.mark.parametrize(
        "body",
        [
            '{"rate_usd_per_credit": Infinity, "source": "x"}',
            '{"rate_usd_per_credit": NaN, "source": "x"}',
            '{"rate_usd_per_credit": 1e400, "source": "x"}',
            '{"rate_usd_per_credit": -0.001, "source": "x"}',
            '{"rate_usd_per_credit": 0, "source": "x"}',
            '{"rate_usd_per_credit": "0.001", "source": "x"}',
            '{"rate_usd_per_credit": true, "source": "x"}',
            '{"rate_usd_per_credit": 5000, "source": "x"}',
            '{"rate_usd_per_credit": 0.001, "source": ""}',
            '{"rate_usd_per_credit": 0.001}',
        ],
    )
    def test_estimate_rejects_unsafe_numbers(self, fakes, api, body):
        attempt_id = finished_attempt(add_task(), 100)
        resp = api.post(
            f"/api/attempts/{attempt_id}/cost-estimate", content=body, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 422, resp.text
        assert attempt_row(attempt_id)["cost"]["status"] == CostStatus.UNKNOWN

    @pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN", "-1", "1e400", "2000000"])
    def test_reconciliation_rejects_unsafe_numbers(self, fakes, api, value):
        attempt_id = finished_attempt(add_task(), 100)
        resp = api.post(
            f"/api/attempts/{attempt_id}/cost-reconciliation",
            content=f'{{"usd": {value}, "source": "invoice"}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, resp.text
        assert attempt_row(attempt_id)["cost"]["reconciled_usd"] is None

    def test_estimate_refused_without_observed_credits_or_while_running(self, fakes, api):
        task_id = add_task()
        never = start_attempt(PARENT_TASK, task_id, AttemptKind.FIX).attempt_id
        fail_attempt_start(never, "could not start")
        rate = {"rate_usd_per_credit": 0.001, "source": "test-only"}
        assert api.post(f"/api/attempts/{never}/cost-estimate", json=rate).status_code == 409
        running = begin(task_id, AttemptKind.FOLLOW_UP)
        assert api.post(f"/api/attempts/{running}/cost-estimate", json=rate).status_code == 409
        assert (
            api.post(f"/api/attempts/{running}/cost-reconciliation", json={"usd": 1, "source": "x"}).status_code == 409
        )
        assert api.post("/api/attempts/999/cost-estimate", json=rate).status_code == 404
        assert attempt_row(never)["cost"]["status"] == CostStatus.UNKNOWN

    @pytest.mark.parametrize(
        ("rate", "source", "expected"),
        [
            ("0.001", "configured test rate", 0.1),
            ("0.001", "", None),
            ("", "src", None),
            ("abc", "src", None),
            ("inf", "src", None),
            ("nan", "src", None),
            ("-0.5", "src", None),
        ],
    )
    def test_configured_estimate_needs_finite_rate_and_source(self, fakes, monkeypatch, rate, source, expected):
        monkeypatch.setenv("FORGE_USD_PER_CREDIT_ESTIMATE", rate)
        monkeypatch.setenv("FORGE_USD_ESTIMATE_SOURCE", source)
        get_settings.cache_clear()
        attempt_id = finished_attempt(add_task(), 100)

        cost = attempt_row(attempt_id)["cost"]
        if expected is None:
            assert cost["status"] == CostStatus.UNKNOWN and cost["estimated_usd"] is None
        else:
            assert cost["status"] == CostStatus.ESTIMATED
            assert cost["estimated_usd"] == pytest.approx(expected)
            assert cost["estimate_source"] == source
