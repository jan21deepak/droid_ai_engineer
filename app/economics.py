"""Outcome economics: what accepted changes cost, what is unknown, and what is only assumed.

- The population is every implementation task. Failed, retried, stopped and
  not-yet-accepted work stays in every total; a task's restricted candidate
  reviews count toward it. Reviews of existing pull requests are reported apart.
- Factory credits are observations. Dollars are known only from explicit
  per-attempt estimates or reconciliations, and a total is reported only when
  every attempt has one. Cost per accepted change needs that complete total
  and at least one accepted change; otherwise it is ``None`` with the reason.
- Human review/rework minutes and the baseline assumptions are operator input,
  never measurements. The scenario built from them is estimated engineering
  capacity, not realized cash savings.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.attempts import list_attempts
from app.delivery import candidate_reviews, current_approval, delivery_state
from app.economics_models import (
    ASSUMPTIONS_ROW_ID,
    USER_ENTERED,
    EconomicsAssumptions,
    HumanEffort,
    no_human_effort,
)
from app.models import ReviewTask, Task
from app.overview import ACCEPTED_STAGES, DEFINITIONS, StagedTask, staged_tasks, task_stage
from app.review_outcomes import REVIEW_TARGET_CANDIDATE
from app.run_models import PARENT_REVIEW, PARENT_TASK, AttemptStatus, RunAttempt, UsageCompleteness
from app.timeutil import iso_sgt
from app.usage import coverage_of, sum_known, usd_summary

MAX_EFFORT_MINUTES = 100_000.0
MAX_BASELINE_MINUTES = 100_000.0
MAX_HOURLY_USD = 10_000.0

ELAPSED_DEFINITION = (
    "Wall-clock time from when the task was created (issue assigned to Droid) to the operator's approval "
    "of the accepted candidate commit. It includes waiting time and excludes publication, CI and merge."
)
POPULATION_DEFINITION = (
    "Every implementation task. A change is accepted when "
    + DEFINITIONS["accepted"][0].lower()
    + DEFINITIONS["accepted"][1:]
)
SCENARIO_LABEL = "Estimated engineering capacity from user-entered assumptions. It is not realized cash savings and not a measurement."
NOTES = (
    "Factory credits are as reported by the Droid SDK; no dollar amount is inferred from them.",
    "Dollar totals appear only when every attempt has an explicit estimate or reconciled amount.",
    "Human minutes and baseline assumptions are user-entered, not measured.",
    "Agent run failures are not production change failures, and agent runtime is not pull request cycle time.",
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass
class Ledger:
    """Attempts and legacy summaries of one or more parents, each counted once."""

    attempts: list[RunAttempt] = field(default_factory=list)
    history_states: list[str] = field(default_factory=list)
    history_credits: list[float | None] = field(default_factory=list)

    def add_parent(self, session: Session, parent_type: str, parent: Task | ReviewTask) -> None:
        self.attempts.extend(list_attempts(session, parent_type, parent.id))
        if not parent.attempts_tracked:
            tokens = parent.estimated_tokens if isinstance(parent, Task) else None
            recorded = parent.factory_credits is not None or tokens is not None
            self.history_states.append(UsageCompleteness.PARTIAL if recorded else UsageCompleteness.UNKNOWN)
            self.history_credits.append(parent.factory_credits)

    def extend(self, other: Ledger) -> None:
        self.attempts.extend(other.attempts)
        self.history_states.extend(other.history_states)
        self.history_credits.extend(other.history_credits)

    @property
    def coverage(self) -> str:
        return coverage_of(
            [a.usage_completeness or UsageCompleteness.UNKNOWN for a in self.attempts] + self.history_states
        )

    @property
    def credits(self) -> float | None:
        return sum_known([a.factory_credits for a in self.attempts] + self.history_credits)

    def usd(self) -> dict[str, Any]:
        return usd_summary(self.attempts, has_history=bool(self.history_states))

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for attempt in self.attempts:
            counts[attempt.status] = counts.get(attempt.status, 0) + 1
        unsuccessful = [a.factory_credits for a in self.attempts if a.status != AttemptStatus.COMPLETED]
        return {
            "observed_factory_credits": self.credits,
            "credit_coverage": self.coverage,
            "attempt_count": len(self.attempts),
            "attempt_status_counts": counts,
            "unsuccessful_attempt_count": len(unsuccessful),
            "unsuccessful_attempt_factory_credits": sum_known(unsuccessful),
            "legacy_summaries": len(self.history_states),
            "usd": self.usd(),
        }


def task_ledgers(session: Session, task: Task) -> tuple[Ledger, Ledger, int]:
    """(implementation, candidate reviews, number of candidate reviews) for one task."""
    implementation = Ledger()
    implementation.add_parent(session, PARENT_TASK, task)
    reviews = Ledger()
    found = candidate_reviews(session, task.id)
    for review in found:
        reviews.add_parent(session, PARENT_REVIEW, review)
    return implementation, reviews, len(found)


def task_elapsed(session: Session, task: Task, accepted: bool) -> dict[str, Any]:
    approval = current_approval(session, task) if accepted else None
    start = _aware(task.created_at)
    end = _aware(approval.approved_at) if approval is not None else None
    seconds = (end - start).total_seconds() if start and end and end >= start else None
    return {
        "start": "task_created",
        "end": "operator_approval",
        "started_at": iso_sgt(start),
        "ended_at": iso_sgt(end),
        "seconds": seconds,
        "detail": None if seconds is not None else "not accepted yet, so there is no approval endpoint",
    }


def _effort(session: Session, task_id: int) -> HumanEffort | None:
    return session.get(HumanEffort, task_id)


def task_economics(session: Session, task: Task, stage: str | None = None) -> dict[str, Any]:
    if stage is None:
        stage = task_stage(task, delivery_state(session, task))
    accepted = stage in ACCEPTED_STAGES
    implementation, reviews, review_count = task_ledgers(session, task)
    combined = Ledger()
    combined.extend(implementation)
    combined.extend(reviews)
    effort = _effort(session, task.id)
    return {
        "task_id": task.id,
        "stage": stage,
        "accepted": accepted,
        "accepted_definition": DEFINITIONS["accepted"],
        **combined.summary(),
        "implementation": implementation.summary(),
        "candidate_reviews": {"count": review_count, **reviews.summary()},
        "elapsed": task_elapsed(session, task, accepted),
        "elapsed_definition": ELAPSED_DEFINITION,
        "human_effort": effort.to_dict() if effort else no_human_effort(),
        "notes": list(NOTES),
    }


def get_assumptions(session: Session) -> EconomicsAssumptions | None:
    return session.get(EconomicsAssumptions, ASSUMPTIONS_ROW_ID)


def _elapsed_summary(samples: list[float]) -> dict[str, Any]:
    return {
        "definition": ELAPSED_DEFINITION,
        "start": "task_created",
        "end": "operator_approval",
        "sample": len(samples),
        "median_seconds": statistics.median(samples) if samples else None,
        "average_seconds": sum(samples) / len(samples) if samples else None,
        "min_seconds": min(samples) if samples else None,
        "max_seconds": max(samples) if samples else None,
    }


def _cost_per_accepted(ledger: Ledger, tasks: int, accepted: int) -> dict[str, Any]:
    usd = ledger.usd()
    population = (
        f"All implementation and candidate-review attempts across {tasks} task(s), including failed, retried, "
        f"stopped and not-yet-accepted work, divided by {accepted} accepted change(s). "
        "Reviews of existing pull requests are excluded."
    )
    result: dict[str, Any] = {"denominator": accepted, "population": population, "usd": None, "credits": None}
    if accepted == 0:
        return {
            **result,
            "state": "no_accepted_changes",
            "detail": "No accepted changes yet, so there is no denominator.",
        }
    if ledger.coverage == UsageCompleteness.COMPLETE and ledger.credits is not None:
        result["credits"] = ledger.credits / accepted
    if usd["total_usd"] is None:
        missing = usd["attempts_unknown"] + (1 if ledger.history_states else 0)
        detail = (
            f"{missing} attempt(s) or legacy summaries have no dollar value, so the cost is unknown. "
            "Record an explicit estimate or reconciled amount on each attempt."
        )
        return {**result, "state": "cost_incomplete", "detail": detail}
    return {
        **result,
        "usd": usd["total_usd"] / accepted,
        "state": "available",
        "detail": "Every attempt has an explicit estimate or reconciled amount; this is not a bill.",
    }


def _scenario(
    assumptions: EconomicsAssumptions | None,
    accepted_efforts: list[HumanEffort | None],
    agent_usd: float | None,
) -> dict[str, Any]:
    accepted = len(accepted_efforts)
    missing = sum(1 for e in accepted_efforts if e is None)
    result: dict[str, Any] = {
        "kind": "estimated_capacity",
        "label": SCENARIO_LABEL,
        "not_cash_savings": True,
        "accepted_without_effort": missing,
        "baseline_minutes": None,
        "recorded_human_minutes": None,
        "estimated_capacity_minutes": None,
        "estimated_capacity_hours": None,
        "estimated_capacity_value_usd": None,
        "agent_usd_known": agent_usd is not None,
        "net_of_agent_cost_usd": None,
        "formula": (
            "accepted changes × baseline minutes per change − user-entered review and rework minutes on those changes"
        ),
    }
    if assumptions is None:
        return {**result, "state": "no_assumptions", "detail": "Enter baseline assumptions to see a scenario."}
    if accepted == 0:
        return {**result, "state": "no_accepted_changes", "detail": "No accepted changes to apply the baseline to."}
    if missing:
        detail = f"{missing} accepted change(s) have no recorded human review/rework minutes; record them first."
        return {**result, "state": "human_effort_missing", "detail": detail}
    baseline = accepted * assumptions.baseline_minutes_per_change
    human = sum(e.review_minutes + e.rework_minutes for e in accepted_efforts if e is not None)
    capacity = baseline - human
    value = capacity / 60.0 * assumptions.loaded_hourly_usd if assumptions.loaded_hourly_usd is not None else None
    return {
        **result,
        "state": "available",
        "detail": "Scenario only: it depends entirely on the assumptions above.",
        "baseline_minutes": baseline,
        "recorded_human_minutes": human,
        "estimated_capacity_minutes": capacity,
        "estimated_capacity_hours": capacity / 60.0,
        "estimated_capacity_value_usd": value,
        "net_of_agent_cost_usd": value - agent_usd if value is not None and agent_usd is not None else None,
    }


def _pr_reviews(session: Session) -> dict[str, Any]:
    ledger = Ledger()
    reviews = [
        r for r in session.query(ReviewTask).all() if (r.review_target or "pull_request") != REVIEW_TARGET_CANDIDATE
    ]
    for review in reviews:
        ledger.add_parent(session, PARENT_REVIEW, review)
    return {
        "count": len(reviews),
        "observed_factory_credits": ledger.credits,
        "credit_coverage": ledger.coverage,
        "usd": ledger.usd(),
        "included_in_cost_per_accepted_change": False,
    }


def portfolio_economics(session: Session, staged: list[StagedTask] | None = None) -> dict[str, Any]:
    staged = staged_tasks(session) if staged is None else staged
    every, accepted_ledger, other_ledger = Ledger(), Ledger(), Ledger()
    elapsed: list[float] = []
    accepted_efforts: list[HumanEffort | None] = []
    efforts = {e.task_id: e for e in session.query(HumanEffort).all()}
    for task, _, stage in staged:
        implementation, reviews, _ = task_ledgers(session, task)
        accepted = stage in ACCEPTED_STAGES
        target = accepted_ledger if accepted else other_ledger
        for ledger in (implementation, reviews):
            every.extend(ledger)
            target.extend(ledger)
        if accepted:
            accepted_efforts.append(efforts.get(task.id))
            seconds = task_elapsed(session, task, True)["seconds"]
            if seconds is not None:
                elapsed.append(seconds)
    task_ids = {task.id for task, _, _ in staged}
    recorded = [e for tid, e in efforts.items() if tid in task_ids]
    summary = every.summary()
    usd = summary["usd"]
    assumptions = get_assumptions(session)
    accepted_count = len(accepted_efforts)
    return {
        "population": {
            "tasks": len(staged),
            "accepted": accepted_count,
            "not_accepted": len(staged) - accepted_count,
            "definition": POPULATION_DEFINITION,
        },
        "credits": {
            "observed": summary["observed_factory_credits"],
            "coverage": summary["credit_coverage"],
            "accepted_tasks": accepted_ledger.credits,
            "not_accepted_tasks": other_ledger.credits,
            "unsuccessful_attempts": summary["unsuccessful_attempt_factory_credits"],
            "unsuccessful_attempt_count": summary["unsuccessful_attempt_count"],
            "attempts": summary["attempt_count"],
            "attempt_status_counts": summary["attempt_status_counts"],
            "legacy_summaries": summary["legacy_summaries"],
            "unit": "Factory credits as reported by the Droid SDK",
        },
        "usd": usd,
        "cost_per_accepted_change": _cost_per_accepted(every, len(staged), accepted_count),
        "elapsed": _elapsed_summary(elapsed),
        "human_effort": {
            "tasks_recorded": len(recorded),
            "review_minutes": sum_known([e.review_minutes for e in recorded]),
            "rework_minutes": sum_known([e.rework_minutes for e in recorded]),
            "accepted_with_effort": sum(1 for e in accepted_efforts if e is not None),
            "accepted_without_effort": sum(1 for e in accepted_efforts if e is None),
            "source": USER_ENTERED,
            "measured": False,
        },
        "assumptions": assumptions.to_dict() if assumptions else None,
        "scenario": _scenario(assumptions, accepted_efforts, usd["total_usd"]),
        "pr_reviews": _pr_reviews(session),
        "notes": list(NOTES),
    }


__all__ = [
    "ELAPSED_DEFINITION",
    "MAX_BASELINE_MINUTES",
    "MAX_EFFORT_MINUTES",
    "MAX_HOURLY_USD",
    "Ledger",
    "get_assumptions",
    "portfolio_economics",
    "task_economics",
]
