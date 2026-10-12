"""Sparse SDK usage: categories missing from later snapshots or a terminal result.

The SDK may omit a category (for example ``factory_credits=None``) from a
``TokenUsageUpdate`` or a terminal result. A missing category is unknown in
that message, not zero, so values already observed for it in this turn must
survive, both through the recorder's persistence throttle and in the stored
snapshot. Terminal values still replace what snapshots suggested, category by
category, and a cumulative snapshot is never added on top of a terminal value.
"""

import asyncio
from functools import partial

import pytest
from droid_sdk.errors import RunTimeoutError

from app import recovery, worker
from app.attempts import retain_late_usage, stop_attempt
from app.database import db_session
from app.droid_client import DroidClient
from app.run_events import TurnRecorder
from app.run_models import AttemptKind, AttemptStatus, RunAttempt, UsageCompleteness, UsageSource
from app.usage import record_snapshot, record_terminal_usage
from tests.test_run_attempts import add_task
from tests.test_run_usage import (
    ScriptedSession,
    accounting,
    attempt_row,
    begin,
    failure,
    fakes,  # noqa: F401  (fixture)
    snapshot,
    success,
    usage,
)

# 0: every snapshot is persisted; 5: a frozen clock coalesces all but the first into one flush.
INTERVALS = pytest.mark.parametrize("interval", [0, 5], ids=["unthrottled", "throttled"])


def run_turn(task_id, attempt_id, session, *, kind, interval):
    on_complete = partial(worker.finalize_fix_task, task_id, attempt_id=attempt_id)
    recorder = TurnRecorder(attempt_id, clock=lambda: 100.0, snapshot_interval=interval)
    asyncio.run(DroidClient()._run_turn(session, "prompt", kind=kind, on_complete=on_complete, observer=recorder))
    return on_complete


def timeout():
    return RunTimeoutError("The run exceeded its timeout", timeout_duration=600)


class TestSparseSnapshots:
    @INTERVALS
    def test_fresh_session_credit_survives_tokens_only_snapshots_and_timeout(self, fakes, interval):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FIX)
        messages = [snapshot(6, inp=50, out=4), snapshot(None, inp=80, out=9), snapshot(None, inp=90, out=11)]
        run_turn(
            task_id, attempt_id, ScriptedSession("sess-1", messages, error=timeout()), kind="fix", interval=interval
        )

        row = attempt_row(attempt_id)
        assert row["status"] == AttemptStatus.FAILED
        assert (row["usage"]["factory_credits"], row["usage"]["input_tokens"], row["usage"]["output_tokens"]) == (
            6,
            90,
            11,
        )
        assert row["usage"]["completeness"] == UsageCompleteness.PARTIAL
        assert row["usage"]["source"] == UsageSource.SNAPSHOT_FRESH_SESSION
        assert row["usage"]["snapshot"]["latest"]["factory_credits"] == 6
        assert accounting(task_id)["attempt_factory_credits"] == 6

    @INTERVALS
    def test_tokens_only_terminal_failure_keeps_snapshot_credits_as_partial(self, fakes, interval):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FIX)
        messages = [snapshot(6, inp=50), snapshot(None, inp=70)]
        result = failure("sess-1", usage(None, inp=90, out=12))
        run_turn(task_id, attempt_id, ScriptedSession("sess-1", messages, result=result), kind="fix", interval=interval)

        row = attempt_row(attempt_id)
        assert row["status"] == AttemptStatus.FAILED
        # Terminal tokens replace the snapshot's; the missing credit category keeps the snapshot's 6.
        assert (row["usage"]["factory_credits"], row["usage"]["input_tokens"], row["usage"]["output_tokens"]) == (
            6,
            90,
            12,
        )
        assert row["usage"]["completeness"] == UsageCompleteness.PARTIAL
        assert row["usage"]["source"] == UsageSource.TERMINAL_WITH_SNAPSHOT
        totals = accounting(task_id)
        assert totals["attempt_factory_credits"] == 6
        assert totals["coverage"] == UsageCompleteness.PARTIAL

    @INTERVALS
    def test_resumed_credit_first_seen_midway_is_its_own_baseline(self, fakes, interval):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FOLLOW_UP)
        messages = [
            snapshot(None, inp=10),
            snapshot(100, inp=15),
            snapshot(None, inp=20),
            snapshot(106, inp=25),
            snapshot(None, inp=30),
        ]
        session = ScriptedSession("sess-1", messages, error=timeout())
        run_turn(task_id, attempt_id, session, kind="follow_up", interval=interval)

        row = attempt_row(attempt_id)
        assert row["usage"]["factory_credits"] == 6
        assert row["usage"]["input_tokens"] == 20
        assert row["usage"]["source"] == UsageSource.SNAPSHOT_DELTA_LOWER_BOUND
        assert row["usage"]["completeness"] == UsageCompleteness.PARTIAL
        stored = row["usage"]["snapshot"]
        assert (stored["first"]["factory_credits"], stored["latest"]["factory_credits"]) == (100, 106)
        assert stored["count"] == 5

    @INTERVALS
    def test_lone_resumed_credit_observation_is_unattributable(self, fakes, interval):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FOLLOW_UP)
        messages = [snapshot(None, inp=10), snapshot(100, inp=15), snapshot(None, inp=20)]
        session = ScriptedSession("sess-1", messages, error=timeout())
        run_turn(task_id, attempt_id, session, kind="follow_up", interval=interval)

        row = attempt_row(attempt_id)
        assert row["usage"]["factory_credits"] is None
        assert row["usage"]["input_tokens"] == 10
        assert row["usage"]["completeness"] == UsageCompleteness.PARTIAL
        assert row["usage"]["snapshot"]["latest"]["factory_credits"] == 100

    def test_restart_keeps_credit_seen_before_tokens_only_snapshots(self, fakes):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FIX, session_id="sess-crash")
        recorder = TurnRecorder(attempt_id, snapshot_interval=0)
        recorder.started("fix")
        recorder.observe(snapshot(6, inp=50))
        recorder.observe(snapshot(None, inp=60))
        # The process dies here: no finished() and no terminal result.

        assert asyncio.run(recovery.recover_interrupted_tasks(droid=fakes)) == 1
        row = attempt_row(attempt_id)
        assert (row["usage"]["factory_credits"], row["usage"]["input_tokens"]) == (6, 60)
        assert row["usage"]["completeness"] == UsageCompleteness.PARTIAL


class TestSparseTerminalResults:
    @INTERVALS
    def test_terminal_zero_and_values_replace_snapshot_values_once(self, fakes, interval):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FIX)
        messages = [snapshot(6, inp=50), snapshot(None, inp=70)]
        on_complete = run_turn(
            task_id,
            attempt_id,
            ScriptedSession("sess-1", messages, result=success("sess-1", usage(0, inp=80, out=3))),
            kind="fix",
            interval=interval,
        )

        row = attempt_row(attempt_id)
        assert (row["usage"]["factory_credits"], row["usage"]["input_tokens"]) == (0, 80)
        assert row["usage"]["source"] == UsageSource.TERMINAL_RESULT
        assert row["usage"]["completeness"] == UsageCompleteness.COMPLETE
        asyncio.run(on_complete({"success": True, "usage": {"factory_credits": 9, "input_tokens": 1}}))
        assert attempt_row(attempt_id)["usage"]["factory_credits"] == 0
        assert accounting(task_id)["attempt_factory_credits"] == 0

    def test_resumed_terminal_credits_are_not_added_to_snapshot_growth(self, fakes):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FOLLOW_UP)
        messages = [snapshot(100, inp=10), snapshot(None, inp=20), snapshot(106, inp=30)]
        result = success("sess-1", usage(5, inp=4, out=1))
        run_turn(task_id, attempt_id, ScriptedSession("sess-1", messages, result=result), kind="follow_up", interval=5)

        row = attempt_row(attempt_id)
        assert (row["usage"]["factory_credits"], row["usage"]["input_tokens"]) == (5, 4)
        assert row["usage"]["source"] == UsageSource.TERMINAL_RESULT

    def test_tokens_only_terminal_without_snapshot_credits_leaves_credits_unknown(self, fakes):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FOLLOW_UP)
        messages = [snapshot(100, inp=10), snapshot(None, inp=20)]
        result = success("sess-1", usage(None, inp=4, out=1))
        run_turn(task_id, attempt_id, ScriptedSession("sess-1", messages, result=result), kind="follow_up", interval=0)

        row = attempt_row(attempt_id)
        # A single resumed credit observation is not attributable, so nothing fills the terminal's gap.
        assert row["usage"]["factory_credits"] is None
        assert (row["usage"]["input_tokens"], row["usage"]["output_tokens"]) == (4, 1)
        assert row["usage"]["source"] == UsageSource.TERMINAL_RESULT
        assert row["usage"]["completeness"] == UsageCompleteness.COMPLETE

    def test_invalid_terminal_credit_falls_back_to_attributable_snapshot(self):
        attempt = RunAttempt(kind=AttemptKind.FIX)
        assert record_snapshot(attempt, {"factory_credits": 6, "input_tokens": 50})
        assert record_snapshot(attempt, {"input_tokens": 70})
        assert record_terminal_usage(attempt, {"factory_credits": float("nan"), "input_tokens": 90})
        assert (attempt.factory_credits, attempt.input_tokens) == (6, 90)
        assert (attempt.usage_source, attempt.usage_completeness) == (
            UsageSource.TERMINAL_WITH_SNAPSHOT,
            UsageCompleteness.PARTIAL,
        )
        # A repeated terminal delivery changes nothing.
        assert not record_terminal_usage(attempt, {"factory_credits": 50, "input_tokens": 1})
        assert (attempt.factory_credits, attempt.input_tokens) == (6, 90)

    def test_late_tokens_only_result_of_a_stopped_turn_keeps_credits_and_stays_stopped(self, fakes):  # noqa: F811
        task_id = add_task()
        attempt_id = begin(task_id, AttemptKind.FIX)
        recorder = TurnRecorder(attempt_id, snapshot_interval=0)
        recorder.observe(snapshot(6, inp=50))
        recorder.observe(snapshot(None, inp=60))
        with db_session() as session:
            stop_attempt(session, session.get(RunAttempt, attempt_id), "operator", "stopped by operator")
        assert attempt_row(attempt_id)["usage"]["factory_credits"] == 6

        late = {"success": True, "usage": {"input_tokens": 90, "output_tokens": 2, "factory_credits": None}}
        assert retain_late_usage(attempt_id, late)
        assert not retain_late_usage(attempt_id, late)
        row = attempt_row(attempt_id)
        assert row["status"] == AttemptStatus.STOPPED
        assert (row["usage"]["factory_credits"], row["usage"]["input_tokens"]) == (6, 90)
        assert row["usage"]["completeness"] == UsageCompleteness.PARTIAL
        assert accounting(task_id)["attempt_factory_credits"] == 6
