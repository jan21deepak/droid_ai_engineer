"""Factory-credit coverage is derived from credit observations, not from generic token completeness.

Attempt rows are inserted directly as labeled fixtures; accepted tasks use the
local Git fixtures in ``tests/delivery_fixtures.py``. No SDK or GitHub call is made.
"""

import json

import pytest

from app.database import db_session
from app.economics import Ledger
from app.models import ReviewTask, Task, TaskStatus
from app.run_models import (
    PARENT_REVIEW,
    PARENT_TASK,
    AttemptKind,
    AttemptStatus,
    RunAttempt,
    UsageCompleteness,
    UsageSource,
)
from app.usage import attempt_credit_state, record_snapshot, record_terminal_usage
from tests.delivery_fixtures import configure
from tests.test_economics import accept, add_pr_review, economics
from tests.test_operator_ui import add_task


def add_usage(
    parent_id: int,
    sequence: int,
    *,
    credits: float | None,
    source: str | None = UsageSource.TERMINAL_RESULT,
    completeness: str = UsageCompleteness.COMPLETE,
    status: str = AttemptStatus.COMPLETED,
    kind: str = AttemptKind.FIX,
    parent_type: str = PARENT_TASK,
    tokens: int | None = 1200,
    snapshot: dict | None = None,
) -> int:
    """Labeled fixture: one finished attempt with the given credit and token observations."""
    with db_session() as session:
        attempt = RunAttempt(
            parent_type=parent_type,
            parent_id=parent_id,
            kind=kind,
            sequence=sequence,
            status=status,
            factory_credits=credits,
            input_tokens=tokens,
            usage_source=source,
            usage_completeness=completeness,
            usage_snapshot=json.dumps(snapshot) if snapshot is not None else None,
        )
        session.add(attempt)
        session.flush()
        return attempt.id


def tokens_only(parent_id: int, sequence: int, **kwargs) -> int:
    """A valid terminal result that reported tokens but no Factory credits: generically complete."""
    return add_usage(parent_id, sequence, credits=None, **kwargs)


def detail(client, task_id: int) -> dict:
    return client.get(f"/api/tasks/{task_id}/economics").json()


class TestAttemptCreditState:
    def make(self, **values) -> RunAttempt:
        defaults = {"parent_type": PARENT_TASK, "parent_id": 1, "kind": AttemptKind.FIX, "sequence": 1}
        return RunAttempt(**{**defaults, **values})

    def test_terminal_credits_including_zero_are_complete(self):
        for credits in (10.0, 0.0):
            attempt = self.make(factory_credits=credits, usage_source=UsageSource.TERMINAL_RESULT)
            assert attempt_credit_state(attempt) == UsageCompleteness.COMPLETE

    def test_tokens_only_terminal_result_is_unknown_credit(self):
        attempt = self.make(
            factory_credits=None,
            input_tokens=10,
            usage_source=UsageSource.TERMINAL_RESULT,
            usage_completeness=UsageCompleteness.COMPLETE,
        )
        assert attempt_credit_state(attempt) == UsageCompleteness.UNKNOWN

    def test_proven_not_started_is_exempt(self):
        attempt = self.make(status=AttemptStatus.BLOCKED, usage_source=UsageSource.NOT_STARTED)
        assert attempt_credit_state(attempt) is None

    def test_start_failure_without_observation_stays_unknown(self):
        attempt = self.make(status=AttemptStatus.FAILED, outcome_subtype="start_failed")
        assert attempt_credit_state(attempt) == UsageCompleteness.UNKNOWN

    @pytest.mark.parametrize("source", [UsageSource.SNAPSHOT_FRESH_SESSION, UsageSource.SNAPSHOT_DELTA_LOWER_BOUND])
    def test_snapshot_credits_are_partial(self, source):
        attempt = self.make(factory_credits=6.0, usage_source=source, usage_completeness=UsageCompleteness.PARTIAL)
        assert attempt_credit_state(attempt) == UsageCompleteness.PARTIAL

    def test_terminal_credits_with_snapshot_filled_tokens_are_complete(self):
        attempt = self.make(kind=AttemptKind.FIX)
        record_snapshot(attempt, {"factory_credits": 4.0, "thinking_tokens": 50})
        assert record_terminal_usage(attempt, {"factory_credits": 4.0, "input_tokens": 100})
        assert attempt.usage_source == UsageSource.TERMINAL_WITH_SNAPSHOT
        assert attempt.usage_completeness == UsageCompleteness.PARTIAL
        assert attempt_credit_state(attempt) == UsageCompleteness.COMPLETE

    def test_snapshot_filled_credits_are_partial(self):
        attempt = self.make(kind=AttemptKind.FIX)
        record_snapshot(attempt, {"factory_credits": 4.0})
        assert record_terminal_usage(attempt, {"input_tokens": 100})
        assert attempt.factory_credits == 4.0
        assert attempt_credit_state(attempt) == UsageCompleteness.PARTIAL

    def test_older_rows_without_a_filled_marker_are_inferred_conservatively(self):
        snapshot = {"first": {"factory_credits": 4.0}, "latest": {"factory_credits": 4.0}, "count": 1}
        attempt = self.make(
            factory_credits=4.0, usage_source=UsageSource.TERMINAL_WITH_SNAPSHOT, usage_snapshot=json.dumps(snapshot)
        )
        assert attempt_credit_state(attempt) == UsageCompleteness.PARTIAL
        no_credit_snapshot = {"first": {"input_tokens": 5}, "latest": {"input_tokens": 5}, "count": 1}
        attempt.usage_snapshot = json.dumps(no_credit_snapshot)
        assert attempt_credit_state(attempt) == UsageCompleteness.COMPLETE


class TestLedger:
    def test_known_ten_plus_tokens_only_is_partial_not_complete(self):
        """The scrutiny reproduction: credits 10 and None, both generically complete."""
        ledger = Ledger(
            attempts=[
                RunAttempt(factory_credits=10.0, usage_source="terminal_result", usage_completeness="complete"),
                RunAttempt(factory_credits=None, usage_source="terminal_result", usage_completeness="complete"),
            ]
        )
        summary = ledger.summary()
        assert summary["observed_factory_credits"] == 10.0
        assert summary["credit_coverage"] == "partial"
        assert summary["usage_coverage"] == "complete"
        assert summary["credit_attempts"]["unknown"] == 1
        assert summary["credit_attempts"]["observed"] == 1

    def test_empty_population_is_unknown_not_observed_zero(self):
        summary = Ledger().summary()
        assert (summary["observed_factory_credits"], summary["credit_coverage"]) == (None, "unknown")

    def test_only_unknown_entries_are_unknown(self):
        ledger = Ledger(attempts=[RunAttempt(factory_credits=None, usage_source="terminal_result")])
        assert ledger.credit_coverage == "unknown"


class TestAcceptedPopulation:
    def test_known_ten_and_missing_credit_attempt_withhold_credits_per_accepted(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        task_id, _ = accept(client, remote)
        add_usage(task_id, 1, credits=10)
        tokens_only(task_id, 2, kind=AttemptKind.FOLLOW_UP)

        econ = economics(client)
        assert econ["credits"]["observed"] == 10
        assert econ["credits"]["coverage"] == "partial"
        assert econ["credits"]["attempts_without_observed_credits"] == 1
        cpa = econ["cost_per_accepted_change"]
        assert cpa["denominator"] == 1
        assert cpa["credits"] is None
        assert "credit" in cpa["credits_detail"].lower() and "partial" in cpa["credits_detail"]

        task = detail(client, task_id)
        assert (task["observed_factory_credits"], task["credit_coverage"]) == (10, "partial")
        assert task["usage_coverage"] == "complete"
        assert task["credits_per_accepted_change"] is None

        export = client.get(f"/api/tasks/{task_id}/evidence").json()["usage"]
        assert (export["observed_factory_credits"], export["credit_coverage"]) == (10, "partial")
        assert export["credits_per_accepted_change"] is None
        assert export["credit_attempts"]["unknown"] == 1

    def test_known_zero_is_complete_and_divides(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        task_id, _ = accept(client, remote)
        add_usage(task_id, 1, credits=0)

        econ = economics(client)
        assert (econ["credits"]["observed"], econ["credits"]["coverage"]) == (0, "complete")
        assert econ["cost_per_accepted_change"]["credits"] == 0
        assert detail(client, task_id)["credits_per_accepted_change"] == 0

    def test_no_start_exemption_keeps_coverage_complete_without_inventing_an_observation(
        self, client, monkeypatch, tmp_path
    ):
        remote = configure(monkeypatch, tmp_path)
        task_id, _ = accept(client, remote)
        add_usage(task_id, 1, credits=10)
        add_usage(task_id, 2, credits=None, tokens=None, source=UsageSource.NOT_STARTED, status=AttemptStatus.BLOCKED)

        econ = economics(client)
        assert (econ["credits"]["observed"], econ["credits"]["coverage"]) == (10, "complete")
        assert econ["credits"]["attempts_exempt_not_started"] == 1
        assert econ["cost_per_accepted_change"]["credits"] == 10
        with db_session() as session:
            blocked = session.query(RunAttempt).filter(RunAttempt.sequence == 2).one()
            assert blocked.factory_credits is None

    def test_only_not_started_work_is_not_observed_zero(self, client):
        task_id = add_task(status=TaskStatus.BLOCKED)
        add_usage(task_id, 1, credits=None, tokens=None, source=UsageSource.NOT_STARTED, status=AttemptStatus.BLOCKED)
        task = detail(client, task_id)
        assert (task["observed_factory_credits"], task["credit_coverage"]) == (None, "unknown")

    def test_failed_and_retried_work_with_observed_credits_is_complete(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        task_id, _ = accept(client, remote)
        add_usage(task_id, 1, credits=3, status=AttemptStatus.FAILED)
        add_usage(task_id, 2, credits=4, kind=AttemptKind.FOLLOW_UP)

        econ = economics(client)
        assert (econ["credits"]["observed"], econ["credits"]["coverage"]) == (7, "complete")
        assert econ["credits"]["unsuccessful_attempts"] == 3
        assert econ["cost_per_accepted_change"]["credits"] == 7

    def test_snapshot_lower_bound_is_partial(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        task_id, _ = accept(client, remote)
        add_usage(task_id, 1, credits=10)
        add_usage(
            task_id,
            2,
            credits=6,
            kind=AttemptKind.FOLLOW_UP,
            source=UsageSource.SNAPSHOT_DELTA_LOWER_BOUND,
            completeness=UsageCompleteness.PARTIAL,
            status=AttemptStatus.INTERRUPTED,
        )
        econ = economics(client)
        assert (econ["credits"]["observed"], econ["credits"]["coverage"]) == (16, "partial")
        assert econ["cost_per_accepted_change"]["credits"] is None

    def test_candidate_review_without_credits_makes_the_task_partial(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        task_id, _ = accept(client, remote)
        add_usage(task_id, 1, credits=10)
        with db_session() as session:
            review_id = session.query(ReviewTask).filter(ReviewTask.task_id == task_id).one().id
        tokens_only(review_id, 1, kind=AttemptKind.REVIEW, parent_type=PARENT_REVIEW)

        task = detail(client, task_id)
        assert (task["observed_factory_credits"], task["credit_coverage"]) == (10, "partial")
        assert task["implementation"]["credit_coverage"] == "complete"
        assert task["candidate_reviews"]["credit_coverage"] == "unknown"
        assert economics(client)["cost_per_accepted_change"]["credits"] is None


class TestLegacyHistory:
    def legacy_task(self, **values) -> int:
        task_id = add_task(**values)
        with db_session() as session:
            task = session.get(Task, task_id)
            assert task is not None
            task.attempts_tracked = None
        return task_id

    def test_legacy_known_credits_are_partial(self, client):
        task_id = self.legacy_task(factory_credits=12.0)
        task = detail(client, task_id)
        assert (task["observed_factory_credits"], task["credit_coverage"]) == (12, "partial")

    def test_legacy_tokens_only_is_unknown(self, client):
        task_id = self.legacy_task(estimated_tokens=5000)
        task = detail(client, task_id)
        assert (task["observed_factory_credits"], task["credit_coverage"]) == (None, "unknown")
        assert task["usage_coverage"] == "partial"

    def test_legacy_unknown_plus_recovery_is_partial(self, client):
        task_id = self.legacy_task()
        add_usage(task_id, 1, credits=7, kind=AttemptKind.RECOVERY)
        task = detail(client, task_id)
        assert (task["observed_factory_credits"], task["credit_coverage"]) == (7, "partial")


class TestPullRequestReviews:
    def test_pr_review_population_has_its_own_credit_coverage(self, client):
        known = add_pr_review()
        add_usage(known, 1, credits=1, kind=AttemptKind.REVIEW, parent_type=PARENT_REVIEW)
        econ = economics(client)
        assert (econ["pr_reviews"]["observed_factory_credits"], econ["pr_reviews"]["credit_coverage"]) == (
            1,
            "complete",
        )

        missing = add_pr_review()
        tokens_only(missing, 1, kind=AttemptKind.REVIEW, parent_type=PARENT_REVIEW)
        econ = economics(client)
        assert (econ["pr_reviews"]["observed_factory_credits"], econ["pr_reviews"]["credit_coverage"]) == (1, "partial")
        assert econ["pr_reviews"]["included_in_cost_per_accepted_change"] is False
        # PR reviews never enter the implementation population.
        assert econ["credits"]["observed"] is None

    def test_usd_coverage_is_unchanged_by_credit_coverage(self, client):
        review_id = add_pr_review()
        tokens_only(review_id, 1, kind=AttemptKind.REVIEW, parent_type=PARENT_REVIEW)
        usd = economics(client)["pr_reviews"]["usd"]
        assert (usd["coverage"], usd["total_usd"], usd["billed_usd"]) == ("unknown", None, None)
