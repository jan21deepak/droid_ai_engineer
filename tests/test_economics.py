"""Outcome economics: accepted population, observed credits, unknown dollars and user-entered inputs.

Accepted tasks come from the labeled local Git fixtures in ``tests/delivery_fixtures.py``;
attempt usage rows are inserted directly as labeled fixtures. No SDK or GitHub call is made.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.database import db_session
from app.models import ReviewTask, Task, TaskStatus
from app.run_models import PARENT_REVIEW, PARENT_TASK, AttemptKind, AttemptStatus, RunAttempt, UsageCompleteness
from tests.delivery_fixtures import add_candidate_review, add_candidate_task, add_check_run, configure
from tests.test_operator_ui import add_task


def add_attempt(
    parent_id: int,
    sequence: int,
    *,
    status: str = AttemptStatus.COMPLETED,
    kind: str = AttemptKind.FIX,
    credits: float | None = None,
    completeness: str = UsageCompleteness.COMPLETE,
    reconciled: float | None = None,
    estimated: float | None = None,
    parent_type: str = PARENT_TASK,
) -> int:
    """Labeled fixture: one finished attempt with the given observed usage and dollar facts."""
    with db_session() as session:
        attempt = RunAttempt(
            parent_type=parent_type,
            parent_id=parent_id,
            kind=kind,
            sequence=sequence,
            status=status,
            factory_credits=credits,
            usage_completeness=completeness,
            usage_source="terminal_result" if completeness == UsageCompleteness.COMPLETE else None,
            reconciled_usd=reconciled,
            reconciliation_source="fixture invoice" if reconciled is not None else None,
            estimated_usd=estimated,
            estimate_rate_usd_per_credit=0.01 if estimated is not None else None,
            estimate_source="fixture rate" if estimated is not None else None,
        )
        session.add(attempt)
        session.flush()
        return attempt.id


def accept(client, remote, *, created_ago: timedelta | None = None) -> tuple[int, str]:
    task_id, sha, _ = add_candidate_task(remote)
    add_check_run(task_id, sha)
    add_candidate_review(task_id, sha, remote.base_sha)
    if created_ago is not None:
        with db_session() as session:
            task = session.get(Task, task_id)
            assert task is not None
            task.created_at = datetime.now(UTC) - created_ago
    assert client.post(f"/api/tasks/{task_id}/approve", json={"candidate_sha": sha}).status_code == 200
    return task_id, sha


def economics(client) -> dict:
    overview = client.get("/api/overview").json()
    direct = client.get("/api/economics").json()
    assert overview["economics"] == direct
    return direct


class TestPopulation:
    def test_empty_dataset_reports_explicit_unknown_and_empty_states(self, client):
        econ = economics(client)
        assert econ["population"]["tasks"] == 0
        assert econ["population"]["accepted"] == 0
        assert econ["credits"]["observed"] is None
        assert econ["credits"]["coverage"] == "unknown"
        assert econ["usd"]["total_usd"] is None
        cpa = econ["cost_per_accepted_change"]
        assert (cpa["state"], cpa["usd"], cpa["credits"], cpa["denominator"]) == ("no_accepted_changes", None, None, 0)
        assert econ["elapsed"]["sample"] == 0 and econ["elapsed"]["median_seconds"] is None
        assert econ["human_effort"]["tasks_recorded"] == 0
        assert econ["assumptions"] is None
        assert econ["scenario"]["state"] == "no_assumptions"
        assert econ["scenario"]["not_cash_savings"] is True

    def test_three_tasks_agree_between_overview_and_detail(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        accepted_id, _ = accept(client, remote)
        add_attempt(accepted_id, 1, credits=10, reconciled=2.0)
        retried = add_task(status=TaskStatus.COMPLETED)
        add_attempt(retried, 1, status=AttemptStatus.FAILED, credits=3)
        add_attempt(retried, 2, kind=AttemptKind.FOLLOW_UP, credits=4, estimated=0.04)
        unknown = add_task(status=TaskStatus.FAILED)
        add_attempt(unknown, 1, status=AttemptStatus.FAILED, credits=None, completeness=UsageCompleteness.UNKNOWN)

        econ = economics(client)
        assert econ["population"] == {
            "tasks": 3,
            "accepted": 1,
            "not_accepted": 2,
            "definition": econ["population"]["definition"],
        }
        assert "approved" in econ["population"]["definition"]
        credits = econ["credits"]
        assert credits["observed"] == 17
        assert credits["accepted_tasks"] == 10
        assert credits["not_accepted_tasks"] == 7
        assert credits["unsuccessful_attempts"] == 3
        assert credits["coverage"] == "partial"
        assert credits["attempt_status_counts"] == {"completed": 2, "failed": 2}
        usd = econ["usd"]
        assert usd["coverage"] == "partial"
        assert usd["total_usd"] is None
        assert usd["known_usd_subtotal"] == pytest.approx(2.04)
        # The failed attempt observed credits but has no dollar value; the unknown task observed nothing.
        assert usd["attempts_unknown"] == 2
        assert usd["billed_usd"] is None
        cpa = econ["cost_per_accepted_change"]
        assert cpa["state"] == "cost_incomplete"
        assert cpa["usd"] is None and cpa["credits"] is None
        assert cpa["denominator"] == 1
        assert "failed" in cpa["population"]

        details = {tid: client.get(f"/api/tasks/{tid}/economics").json() for tid in (accepted_id, retried, unknown)}
        assert [d["accepted"] for d in details.values()] == [True, False, False]
        assert sum(d["observed_factory_credits"] or 0 for d in details.values()) == credits["observed"]
        assert details[unknown]["observed_factory_credits"] is None
        assert details[unknown]["usd"]["total_usd"] is None
        assert details[unknown]["credit_coverage"] == "unknown"
        assert details[retried]["unsuccessful_attempt_factory_credits"] == 3
        assert details[retried]["unsuccessful_attempt_count"] == 1
        assert details[accepted_id]["unsuccessful_attempt_count"] == 0
        assert details[accepted_id]["usd"]["total_usd"] == 2.0

    def test_cost_per_accepted_change_spreads_failed_work_over_accepted_changes(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        accepted_id, _ = accept(client, remote)
        add_attempt(accepted_id, 1, credits=10, reconciled=2.0)
        failed = add_task(status=TaskStatus.FAILED)
        add_attempt(failed, 1, status=AttemptStatus.FAILED, credits=5, reconciled=1.0)

        cpa = economics(client)["cost_per_accepted_change"]
        assert cpa["state"] == "available"
        assert cpa["usd"] == pytest.approx(3.0)
        assert cpa["credits"] == pytest.approx(15)
        assert cpa["denominator"] == 1

    def test_candidate_review_usage_counts_toward_its_task(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        accepted_id, _ = accept(client, remote)
        add_attempt(accepted_id, 1, credits=10)
        with db_session() as session:
            review = session.query(ReviewTask).filter(ReviewTask.task_id == accepted_id).one()
            review_id = review.id
        add_attempt(review_id, 1, kind=AttemptKind.REVIEW, credits=2, parent_type=PARENT_REVIEW)
        pr_review = add_pr_review()
        add_attempt(pr_review, 1, kind=AttemptKind.REVIEW, credits=1, parent_type=PARENT_REVIEW)

        econ = economics(client)
        assert econ["credits"]["observed"] == 12
        assert econ["pr_reviews"]["observed_factory_credits"] == 1
        assert econ["pr_reviews"]["included_in_cost_per_accepted_change"] is False
        detail = client.get(f"/api/tasks/{accepted_id}/economics").json()
        assert detail["candidate_reviews"]["observed_factory_credits"] == 2
        assert detail["observed_factory_credits"] == 12

    def test_elapsed_time_runs_from_assignment_to_operator_approval(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        accepted_id, _ = accept(client, remote, created_ago=timedelta(hours=2))
        add_task()
        elapsed = economics(client)["elapsed"]
        assert elapsed["sample"] == 1
        assert elapsed["median_seconds"] == pytest.approx(7200, abs=60)
        assert "approv" in elapsed["definition"] and "created" in elapsed["definition"]
        detail = client.get(f"/api/tasks/{accepted_id}/economics").json()
        assert detail["elapsed"]["seconds"] == pytest.approx(7200, abs=60)
        assert detail["elapsed"]["end"] == "operator_approval"


def add_pr_review() -> int:
    with db_session() as session:
        review = ReviewTask(repository="jan21deepak/droid_ai_engineer", pr_number=3, status=TaskStatus.COMPLETED)
        session.add(review)
        session.flush()
        return review.id


class TestUserEnteredInputs:
    def test_human_effort_persists_and_is_labeled_user_entered(self, client):
        task_id = add_task()
        response = client.put(
            f"/api/tasks/{task_id}/human-effort",
            json={"review_minutes": 30, "rework_minutes": 12.5, "note": "review <b>notes</b>"},
        )
        assert response.status_code == 200, response.text
        effort = client.get(f"/api/tasks/{task_id}/economics").json()["human_effort"]
        assert effort["review_minutes"] == 30
        assert effort["rework_minutes"] == 12.5
        assert effort["note"] == "review <b>notes</b>"
        assert effort["source"] == "user_entered"
        assert effort["measured"] is False
        assert effort["updated_at"]
        totals = economics(client)["human_effort"]
        assert (totals["tasks_recorded"], totals["review_minutes"], totals["rework_minutes"]) == (1, 30, 12.5)
        assert totals["source"] == "user_entered"

    @pytest.mark.parametrize(
        "body",
        [
            {"review_minutes": -1, "rework_minutes": 0},
            {"review_minutes": 1e9, "rework_minutes": 0},
            {"review_minutes": "30", "rework_minutes": 0},
            {"review_minutes": True, "rework_minutes": 0},
            {"review_minutes": 1, "rework_minutes": 0, "note": "x" * 201},
            {"rework_minutes": 0},
        ],
    )
    def test_human_effort_rejects_invalid_input(self, client, body):
        task_id = add_task()
        assert client.put(f"/api/tasks/{task_id}/human-effort", json=body).status_code == 422
        assert client.get(f"/api/tasks/{task_id}/economics").json()["human_effort"]["recorded"] is False

    def test_human_effort_rejects_nonfinite_numbers_and_unknown_tasks(self, client):
        task_id = add_task()
        raw = '{"review_minutes": NaN, "rework_minutes": Infinity}'
        response = client.put(
            f"/api/tasks/{task_id}/human-effort", content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422
        assert "NaN" not in response.text
        assert (
            client.put("/api/tasks/9999/human-effort", json={"review_minutes": 1, "rework_minutes": 1}).status_code
            == 404
        )
        assert client.get("/api/tasks/9999/economics").status_code == 404

    def test_assumptions_persist_and_scenario_is_estimated_capacity(self, client, monkeypatch, tmp_path):
        remote = configure(monkeypatch, tmp_path)
        accepted_id, _ = accept(client, remote)
        add_attempt(accepted_id, 1, credits=10)
        body = {"baseline_minutes_per_change": 240, "loaded_hourly_usd": 100, "source": "team estimate"}
        assert client.put("/api/economics/assumptions", json=body).status_code == 200
        scenario = economics(client)["scenario"]
        assert scenario["state"] == "human_effort_missing"
        assert scenario["accepted_without_effort"] == 1
        assert scenario["estimated_capacity_minutes"] is None

        effort = {"review_minutes": 30, "rework_minutes": 15}
        assert client.put(f"/api/tasks/{accepted_id}/human-effort", json=effort).status_code == 200
        econ = economics(client)
        assumptions = econ["assumptions"]
        assert assumptions["baseline_minutes_per_change"] == 240
        assert assumptions["loaded_hourly_usd"] == 100
        assert assumptions["source"] == "team estimate"
        assert assumptions["measured"] is False
        scenario = econ["scenario"]
        assert scenario["state"] == "available"
        assert scenario["kind"] == "estimated_capacity"
        assert scenario["not_cash_savings"] is True
        assert scenario["baseline_minutes"] == 240
        assert scenario["recorded_human_minutes"] == 45
        assert scenario["estimated_capacity_minutes"] == 195
        assert scenario["estimated_capacity_value_usd"] == pytest.approx(325.0)
        # Agent dollars are unknown, so nothing is netted against them.
        assert scenario["agent_usd_known"] is False
        assert scenario["net_of_agent_cost_usd"] is None

    @pytest.mark.parametrize(
        "body",
        [
            {"baseline_minutes_per_change": 0, "source": "x"},
            {"baseline_minutes_per_change": -5, "source": "x"},
            {"baseline_minutes_per_change": 1e9, "source": "x"},
            {"baseline_minutes_per_change": 60, "loaded_hourly_usd": -1, "source": "x"},
            {"baseline_minutes_per_change": 60, "source": ""},
            {"baseline_minutes_per_change": 60},
        ],
    )
    def test_assumptions_reject_invalid_input(self, client, body):
        assert client.put("/api/economics/assumptions", json=body).status_code == 422
        assert economics(client)["assumptions"] is None

    def test_economics_routes_require_operator_access(self, protected_client):
        assert protected_client.get("/api/economics").status_code == 401
        assert protected_client.get("/api/tasks/1/economics").status_code == 401
        assert protected_client.put("/api/economics/assumptions", json={}).status_code in (401, 403)
        assert protected_client.put("/api/tasks/1/human-effort", json={}).status_code in (401, 403)


class TestHonestLabels:
    def test_activity_tab_does_not_claim_cash_savings_dora_or_pr_cycle_time(self, client):
        text = client.get("/dashboard").text
        assert "Time-to-Dollar Savings" not in text
        assert "Change Failure Rate" not in text
        assert "Avg PR Cycle Time" not in text
        assert "Agent run failure rate" in text
        assert "not the DORA" in text
        assert "not end-to-end pull request cycle time" in text

    def test_metrics_labels_explain_what_is_measured(self, client):
        labels = client.get("/metrics").json()["labels"]
        assert "not the DORA" in labels["change_failure_rate_percent"]
        assert "not end-to-end" in labels["pr_cycle_time_hours"]
        assert "not realized cash savings" in labels["productivity_gained_usd"]

    def test_overview_shows_economics_with_definitions(self, client):
        text = client.get("/dashboard").text
        for marker in (
            'id="economics"',
            "Accepted changes",
            "Cost per accepted change",
            "Human review effort",
            "Elapsed delivery time",
            "not cash savings",
            "/static/economics.js",
        ):
            assert marker in text, marker
