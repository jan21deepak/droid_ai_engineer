"""Conservative limits checked before every paid Droid turn.

Every implementation, review, follow-up and recovery turn passes the same
admission check, inside the transaction that commits its attempt:

- the configured spending check (``FORGE_BUDGET_POLICY``) fails closed: a
  policy whose required input is missing, whose threshold is exhausted, whose
  data cannot be verified, or whose name is unknown blocks the turn;
- each implementation task gets at most ``FORGE_MAX_CONTINUATIONS`` follow-up
  and recovery turns;
- at most ``FORGE_MAX_CONCURRENT_RUNS`` paid turns are in flight at once,
  counting both active attempts and SDK turns still winding down after a stop.

A durable refusal is recorded as a ``blocked`` attempt (nothing spawned, no
usage) with its reason, so it stays visible and blocked across restarts.

The ``account_cap`` policy records an account-level dollar cap that the account
owner confirmed is enforced by Factory billing. Forge cannot verify or enforce
that cap: Factory credits are observed, but no credit-to-USD rate is known
unless one is explicitly configured as an estimate. Unknown dollars are shown
as unknown, never converted or zeroed, and do not block this policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import ReviewTask, Task, TaskStatus
from app.run_models import AttemptKind, AttemptStatus, RunAttempt, UsageCompleteness, UsageSource
from app.usage import MAX_CREDITS, MAX_USD, configured_estimate_rate, finite_number, sum_known

LAUNCH_KINDS = (AttemptKind.FIX, AttemptKind.REVIEW)
CONTINUATION_KINDS = (AttemptKind.FOLLOW_UP, AttemptKind.RECOVERY)


class RefusalCode:
    PARENT_NOT_ACTIVE = "parent_not_active"
    RUN_CAPACITY = "run_capacity"
    CONTINUATION_LIMIT = "continuation_limit"
    SPENDING_INPUT_MISSING = "spending_check_input_missing"
    SPENDING_LIMIT_REACHED = "spending_limit_reached"
    SPENDING_POLICY_UNKNOWN = "spending_policy_unknown"


class BudgetPolicy:
    # No app-side spending check; only the run, runtime and continuation limits apply.
    NONE = "none"
    # An external account-level USD cap confirmed by the account owner; Forge records it, does not enforce it.
    ACCOUNT_CAP = "account_cap"
    # Forge blocks new turns once observed Factory credits reach FORGE_CREDIT_LIMIT.
    CREDIT_LIMIT = "credit_limit"

    ALL = (NONE, ACCOUNT_CAP, CREDIT_LIMIT)


@dataclass(frozen=True)
class RunRefusal:
    code: str
    detail: str
    # Recorded as a blocked attempt (and a blocked parent for launches) rather than a transient 409.
    durable: bool


class RunRefusedError(Exception):
    """A paid turn that was not started because a run limit or spending check refused it."""

    def __init__(self, refusal: RunRefusal, blocked_attempt_id: int | None = None):
        super().__init__(refusal.detail)
        self.refusal = refusal
        self.code = refusal.code
        self.detail = refusal.detail
        self.blocked_attempt_id = blocked_attempt_id


def max_concurrent_runs(settings: Settings | None = None) -> int:
    return max(1, int((settings or get_settings()).forge_max_concurrent_runs))


def max_continuations(settings: Settings | None = None) -> int:
    return max(0, int((settings or get_settings()).forge_max_continuations))


def runtime_limit_seconds(settings: Settings | None = None) -> float:
    value = float((settings or get_settings()).droid_turn_timeout_seconds or 0)
    return value if value > 0 else 3600.0


def _positive(text: str, maximum: float) -> float | None:
    try:
        value = finite_number(float((text or "").strip()), maximum=maximum)
    except ValueError:
        return None
    return value if value is not None and value > 0 else None


def _live_sessions() -> set[str]:
    from app.droid_client import get_droid_client

    return set(get_droid_client().active_sessions)


def in_flight_runs(session: Session) -> list[str]:
    """Paid turns currently in flight: active attempts plus SDK turns still winding down."""
    active = session.query(RunAttempt).filter(RunAttempt.status.in_(AttemptStatus.ACTIVE)).all()
    keys = {a.session_id or f"attempt:{a.id}" for a in active}
    return sorted(keys | _live_sessions())


def _usage_unverifiable(attempt: RunAttempt) -> bool:
    """A finished attempt that may have used credits Forge never observed."""
    return (
        not attempt.active
        and (attempt.usage_completeness or UsageCompleteness.UNKNOWN) == UsageCompleteness.UNKNOWN
        and attempt.usage_source != UsageSource.NOT_STARTED
        and attempt.outcome_subtype != "start_failed"
    )


def observed_credit_usage(session: Session) -> dict[str, Any]:
    """Factory credits observed across every attempt and legacy summary in this database."""
    attempts = session.query(RunAttempt).all()
    legacy = [t.factory_credits for t in session.query(Task).filter(Task.attempts_tracked.is_(None)).all()]
    legacy += [r.factory_credits for r in session.query(ReviewTask).filter(ReviewTask.attempts_tracked.is_(None)).all()]
    completeness = [a.usage_completeness or UsageCompleteness.UNKNOWN for a in attempts]
    return {
        "observed_factory_credits": sum_known([a.factory_credits for a in attempts] + legacy),
        "attempts_with_unverifiable_usage": sum(1 for a in attempts if _usage_unverifiable(a)),
        "attempts_with_partial_usage": completeness.count(UsageCompleteness.PARTIAL),
        "legacy_summaries": sum(1 for value in legacy if value is not None),
    }


def _usd_view(credits: float | None) -> dict[str, Any]:
    configured = configured_estimate_rate()
    if configured is None or credits is None:
        return {
            "status": "unknown",
            "estimated_usd": None,
            "rate_usd_per_credit": None,
            "rate_source": None,
            "note": "No verified credit-to-USD rate; dollars stay unknown and are never derived from credits.",
        }
    rate, source = configured
    return {
        "status": "estimated",
        "estimated_usd": finite_number(round(credits * rate, 6), maximum=MAX_USD),
        "rate_usd_per_credit": rate,
        "rate_source": source,
        "note": "Explicit operator-configured estimate, not a bill.",
    }


def spending_status(session: Session) -> dict[str, Any]:
    """The configured spending check and whether it currently allows a new paid turn."""
    settings = get_settings()
    policy = (settings.forge_budget_policy or BudgetPolicy.NONE).strip().lower() or BudgetPolicy.NONE
    usage = observed_credit_usage(session)
    credits = usage["observed_factory_credits"]
    status: dict[str, Any] = {
        "policy": policy,
        "allowed": True,
        "code": None,
        "reason": None,
        "enforced_by": "none",
        "account_cap_usd": None,
        "app_enforces_usd_ceiling": False,
        "credit_limit": None,
        **usage,
        "usd": _usd_view(credits),
        "notes": [],
    }

    def refuse(code: str, reason: str) -> dict[str, Any]:
        status.update(allowed=False, code=code, reason=reason)
        return status

    if policy == BudgetPolicy.NONE:
        status["notes"].append("No spending check is configured; run, runtime and continuation limits still apply.")
        return status
    if policy == BudgetPolicy.ACCOUNT_CAP:
        cap = _positive(settings.forge_demo_account_cap_usd, MAX_USD)
        status["enforced_by"] = "factory_account"
        if cap is None:
            return refuse(
                RefusalCode.SPENDING_INPUT_MISSING,
                "FORGE_BUDGET_POLICY=account_cap requires FORGE_DEMO_ACCOUNT_CAP_USD: the account-level USD cap "
                "the account owner confirmed is enforced by Factory billing",
            )
        status["account_cap_usd"] = cap
        status["notes"] += [
            f"Real runs rely on an account-level cap of US${cap:g} confirmed by the account owner and enforced "
            "by Factory billing, not by Forge.",
            "Forge observes Factory credits; without a verified rate it does not know or enforce dollars.",
            "Forge enforces one paid run at a time, a per-turn runtime limit and a continuation limit.",
        ]
        return status
    if policy == BudgetPolicy.CREDIT_LIMIT:
        limit = _positive(settings.forge_credit_limit, MAX_CREDITS)
        status["enforced_by"] = "forge"
        if limit is None:
            return refuse(
                RefusalCode.SPENDING_INPUT_MISSING,
                "FORGE_BUDGET_POLICY=credit_limit requires FORGE_CREDIT_LIMIT (a positive number of Factory credits)",
            )
        status["credit_limit"] = limit
        if usage["attempts_with_unverifiable_usage"]:
            return refuse(
                RefusalCode.SPENDING_INPUT_MISSING,
                f"{usage['attempts_with_unverifiable_usage']} finished attempt(s) have unknown usage, so observed "
                "credits cannot be checked against FORGE_CREDIT_LIMIT",
            )
        if credits is not None and credits >= limit:
            return refuse(
                RefusalCode.SPENDING_LIMIT_REACHED,
                f"observed Factory credits {credits:g} reached FORGE_CREDIT_LIMIT {limit:g}",
            )
        if usage["attempts_with_partial_usage"]:
            status["notes"].append("Some attempts have partial usage; observed credits are a lower bound.")
        return status
    return refuse(
        RefusalCode.SPENDING_POLICY_UNKNOWN,
        f"unknown FORGE_BUDGET_POLICY {policy[:40]!r}; expected one of {', '.join(BudgetPolicy.ALL)}",
    )


def continuations_used(session: Session, parent_type: str, parent_id: int) -> int:
    """Follow-up/recovery turns that were actually started for this parent."""
    return (
        session.query(RunAttempt)
        .filter(
            RunAttempt.parent_type == parent_type,
            RunAttempt.parent_id == parent_id,
            RunAttempt.kind.in_(CONTINUATION_KINDS),
            RunAttempt.status != AttemptStatus.BLOCKED,
            (RunAttempt.outcome_subtype.is_(None)) | (RunAttempt.outcome_subtype != "start_failed"),
        )
        .count()
    )


def admission_refusal(
    session: Session, parent_type: str, parent_id: int, parent_status: str, kind: str
) -> RunRefusal | None:
    """Why a new paid turn of ``kind`` may not start for this parent now, or ``None`` if it may."""
    if kind in LAUNCH_KINDS and parent_status not in TaskStatus.ACTIVE:
        return RunRefusal(
            RefusalCode.PARENT_NOT_ACTIVE, f"{parent_type} is {parent_status}; it is not waiting to start", False
        )
    spending = spending_status(session)
    if not spending["allowed"]:
        return RunRefusal(spending["code"], spending["reason"], True)
    if kind in CONTINUATION_KINDS:
        used, allowed = continuations_used(session, parent_type, parent_id), max_continuations()
        if used >= allowed:
            return RunRefusal(
                RefusalCode.CONTINUATION_LIMIT,
                f"continuation limit reached: {used} of {allowed} follow-up/recovery turn(s) used "
                "(FORGE_MAX_CONTINUATIONS)",
                True,
            )
    running, capacity = in_flight_runs(session), max_concurrent_runs()
    if len(running) >= capacity:
        return RunRefusal(
            RefusalCode.RUN_CAPACITY,
            f"{len(running)} paid run(s) already in flight; FORGE_MAX_CONCURRENT_RUNS={capacity}",
            kind in LAUNCH_KINDS,
        )
    return None


def run_limits_summary(session: Session) -> dict[str, Any]:
    settings = get_settings()
    running = in_flight_runs(session)
    return {
        "max_concurrent_runs": max_concurrent_runs(settings),
        "runs_in_flight": len(running),
        "max_continuations_per_task": max_continuations(settings),
        "runtime_limit_seconds": runtime_limit_seconds(settings),
        "spending": spending_status(session),
    }


__all__ = [
    "CONTINUATION_KINDS",
    "LAUNCH_KINDS",
    "BudgetPolicy",
    "RefusalCode",
    "RunRefusal",
    "RunRefusedError",
    "admission_refusal",
    "continuations_used",
    "in_flight_runs",
    "max_concurrent_runs",
    "max_continuations",
    "observed_credit_usage",
    "run_limits_summary",
    "runtime_limit_seconds",
    "spending_status",
]
