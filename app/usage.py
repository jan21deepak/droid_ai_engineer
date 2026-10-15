"""Per-attempt usage and dollar accounting rules.

- Every usage category is kept separately and ``None`` means *not observed*;
  categories are never added into an invented "billable" total.
- A completed turn's terminal result is that turn's contribution and is
  counted once. TokenUsageUpdate snapshots are session-cumulative: they are
  stored as snapshots and only become a (partial) contribution when no terminal
  result arrived and the snapshot can be attributed to this turn.
- Dollars are never derived implicitly from Factory credits. An estimate needs
  an explicit rate and source; a reconciled value needs a supplied amount and
  source. Both are kept distinct, and unknown is never shown as zero.
- Every number is validated: finite, non-negative and bounded.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.redaction import redact_text
from app.run_models import (
    USAGE_FIELDS,
    AttemptKind,
    CostStatus,
    RunAttempt,
    UsageCompleteness,
    UsageSource,
)

MAX_TOKENS = 10**15
MAX_CREDITS = 1e12
MAX_USD = 1_000_000.0
MAX_USD_PER_CREDIT = 1_000.0
SOURCE_LIMIT = 200

INTEGER_FIELDS = tuple(name for name in USAGE_FIELDS if name != "factory_credits")
# Snapshot key listing the categories a terminal result omitted and snapshots filled in.
FILLED_KEY = "terminal_filled"
# Kinds whose turn opens a brand-new SDK session, so its cumulative snapshot is all this turn's.
FRESH_SESSION_KINDS = (AttemptKind.FIX, AttemptKind.REVIEW)

ATTRIBUTION_LIMITS = (
    "Usage is what the Droid SDK reported to this service; delegated child-session usage is not observed separately.",
    "Snapshot-derived usage (no terminal result) is partial; in resumed sessions it is a lower bound.",
    "Token categories are reported separately and are not summed into a billable total.",
    "No billing connector: dollar values are explicit estimates or manually reconciled values only.",
)


class CostInputError(ValueError):
    """A dollar estimate or reconciliation that cannot be applied to this attempt."""

    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def finite_number(value: Any, *, integer: bool = False, maximum: float | None = None) -> int | float | None:
    """``value`` as a safe non-negative finite number, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0 or (maximum is not None and value > maximum):
        return None
    return int(value) if integer else float(value)


def clean_usage(raw: Mapping[str, Any] | None) -> dict[str, int | float]:
    """Only the validly reported categories of ``raw``; missing or invalid ones are omitted (unknown)."""
    if not raw:
        return {}
    cleaned: dict[str, int | float] = {}
    for name in USAGE_FIELDS:
        integer = name in INTEGER_FIELDS
        value = finite_number(raw.get(name), integer=integer, maximum=MAX_TOKENS if integer else MAX_CREDITS)
        if value is not None:
            cleaned[name] = value
    return cleaned


def _set_usage(attempt: RunAttempt, values: Mapping[str, int | float], *, source: str, completeness: str) -> None:
    for name in USAGE_FIELDS:
        setattr(attempt, name, values.get(name))
    attempt.usage_source = source
    attempt.usage_completeness = completeness
    attempt.usage_recorded_at = datetime.now(UTC)


def record_terminal_usage(attempt: RunAttempt, usage: Mapping[str, Any] | None) -> bool:
    """Store a turn's terminal usage once; a repeat delivery changes nothing.

    Each category the terminal result reports replaces any snapshot-derived
    value, because it is the turn's own total. A category the result omits (or
    reports invalidly) keeps only this turn's attributable snapshot value, and
    the usage is then partial; a cumulative snapshot is never added on top.
    """
    if attempt.usage_source in UsageSource.TERMINAL:
        return False
    values = clean_usage(usage)
    if not values:
        return False
    attributable = attributable_snapshot_usage(attempt)
    filled = {k: v for k, v in attributable[0].items() if k not in values} if attributable else {}
    if filled:
        _set_usage(
            attempt,
            {**filled, **values},
            source=UsageSource.TERMINAL_WITH_SNAPSHOT,
            completeness=UsageCompleteness.PARTIAL,
        )
        stored = _load_snapshot(attempt)
        stored[FILLED_KEY] = sorted(filled)
        attempt.usage_snapshot = json.dumps(stored, sort_keys=True)
    else:
        _set_usage(attempt, values, source=UsageSource.TERMINAL_RESULT, completeness=UsageCompleteness.COMPLETE)
    return True


def _load_snapshot(attempt: RunAttempt) -> dict[str, Any]:
    if not attempt.usage_snapshot:
        return {}
    try:
        raw = json.loads(attempt.usage_snapshot)
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _stored_values(stored: Mapping[str, Any], key: str) -> dict[str, int | float]:
    value = stored.get(key)
    return clean_usage(value if isinstance(value, dict) else None)


def record_snapshot(
    attempt: RunAttempt,
    raw: Mapping[str, Any] | None,
    *,
    first: Mapping[str, Any] | None = None,
    observations: int = 1,
) -> bool:
    """Keep each category's first and latest session-cumulative value seen in this turn. Never a turn charge.

    ``raw`` holds the latest values; ``first`` (default ``raw``) the earliest
    values of the same batch when several observations were coalesced. A
    category missing from an update was not reported in it, so its earlier
    values are kept rather than cleared.
    """
    latest = clean_usage(raw)
    if not latest:
        return False
    earliest = clean_usage(first) if first is not None else latest
    stored = _load_snapshot(attempt)
    merged_first = {**earliest, **_stored_values(stored, "first")}
    for name, value in latest.items():
        merged_first.setdefault(name, value)
    merged_latest = {**_stored_values(stored, "latest"), **latest}
    previous = stored.get("count")
    count = (previous if isinstance(previous, int) else 0) + max(1, observations)
    attempt.usage_snapshot = json.dumps(
        {**stored, "first": merged_first, "latest": merged_latest, "count": count}, sort_keys=True
    )
    attempt.usage_snapshot_at = datetime.now(UTC)
    return True


def attributable_snapshot_usage(attempt: RunAttempt) -> tuple[dict[str, int | float], str] | None:
    """The part of this turn's snapshots that is attributable to the turn, with its source label.

    A fresh session's cumulative snapshot is entirely this turn's. In a resumed
    session the cumulative counter may include earlier turns (or restart with
    the process), so only growth observed during this turn is counted, per
    category from that category's own first observation, as a lower bound. A
    category seen once, or anything else ambiguous, is not invented as a charge.
    """
    stored = _load_snapshot(attempt)
    latest = _stored_values(stored, "latest")
    if not latest:
        return None
    if attempt.kind in FRESH_SESSION_KINDS:
        return latest, UsageSource.SNAPSHOT_FRESH_SESSION
    first = _stored_values(stored, "first")
    delta: dict[str, int | float] = {}
    for name, value in latest.items():
        start = first.get(name)
        if start is not None and value > start:
            delta[name] = value - start
    return (delta, UsageSource.SNAPSHOT_DELTA_LOWER_BOUND) if delta else None


def apply_snapshot_usage(attempt: RunAttempt) -> bool:
    """Without a terminal result, keep attributable snapshot usage as a partial observation."""
    if attempt.usage_source in UsageSource.TERMINAL:
        return False
    attributable = attributable_snapshot_usage(attempt)
    if attributable is None:
        return False
    values, source = attributable
    _set_usage(attempt, values, source=source, completeness=UsageCompleteness.PARTIAL)
    return True


def finalize_usage(attempt: RunAttempt, terminal_usage: Mapping[str, Any] | None) -> None:
    """Terminal usage when reported, otherwise attributable snapshots; then any configured estimate."""
    if not record_terminal_usage(attempt, terminal_usage):
        apply_snapshot_usage(attempt)
    apply_configured_estimate(attempt)


# ---------------------------------------------------------------------------
# Dollars
# ---------------------------------------------------------------------------


def clean_source(value: Any) -> str | None:
    """A bounded, single-line, redacted provenance label, or ``None`` when unusable."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > SOURCE_LIMIT:
        return None
    return redact_text(text)


def set_estimate(attempt: RunAttempt, rate_usd_per_credit: Any, source: Any) -> None:
    """Estimate this attempt's dollars from its observed credits at an explicit rate. Not a bill."""
    rate = finite_number(rate_usd_per_credit, maximum=MAX_USD_PER_CREDIT)
    if rate is None or rate <= 0:
        raise CostInputError("rate_usd_per_credit must be a finite number above 0 and at most 1000")
    label = clean_source(source)
    if label is None:
        raise CostInputError("source must be a non-empty label of at most 200 characters")
    if attempt.active:
        raise CostInputError("attempt is still running; estimate it after it ends", 409)
    if attempt.factory_credits is None:
        raise CostInputError("no Factory credits were observed for this attempt; its cost stays unknown", 409)
    estimate = finite_number(round(attempt.factory_credits * rate, 6), maximum=MAX_USD)
    if estimate is None:
        raise CostInputError("estimate exceeds the supported range")
    attempt.estimated_usd = estimate
    attempt.estimate_rate_usd_per_credit = rate
    attempt.estimate_source = label
    attempt.estimated_at = datetime.now(UTC)


def set_reconciliation(attempt: RunAttempt, usd: Any, source: Any) -> None:
    """Record a manually supplied dollar amount (e.g. from an invoice), kept apart from any estimate."""
    value = finite_number(usd, maximum=MAX_USD)
    if value is None:
        raise CostInputError("usd must be a finite number from 0 to 1000000")
    label = clean_source(source)
    if label is None:
        raise CostInputError("source must be a non-empty label of at most 200 characters")
    if attempt.active:
        raise CostInputError("attempt is still running; reconcile it after it ends", 409)
    attempt.reconciled_usd = value
    attempt.reconciliation_source = label
    attempt.reconciled_at = datetime.now(UTC)


def configured_estimate_rate() -> tuple[float, str] | None:
    """The operator-configured USD-per-credit estimate and its source, or ``None`` unless both are valid."""
    settings = get_settings()
    try:
        configured = float((settings.forge_usd_per_credit_estimate or "").strip())
    except ValueError:
        return None
    rate = finite_number(configured, maximum=MAX_USD_PER_CREDIT)
    source = clean_source(settings.forge_usd_estimate_source)
    if rate is None or rate <= 0 or source is None:
        return None
    return float(rate), source


def apply_configured_estimate(attempt: RunAttempt) -> bool:
    """Apply the operator-configured estimate rate, only when both a valid rate and a source are set."""
    if attempt.estimated_usd is not None or attempt.factory_credits is None:
        return False
    configured = configured_estimate_rate()
    if configured is None:
        return False
    try:
        set_estimate(attempt, *configured)
    except CostInputError:
        return False
    return True


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def sum_known(values: Iterable[Any]) -> float | None:
    known = [float(v) for v in values if v is not None]
    return sum(known) if known else None


def usage_totals(attempts: list[RunAttempt]) -> dict[str, Any]:
    """Per-category sums of observed contributions, with how many attempts observed each category."""
    totals: dict[str, Any] = {}
    observed: dict[str, int] = {}
    for name in USAGE_FIELDS:
        values = [getattr(a, name) for a in attempts if getattr(a, name) is not None]
        observed[name] = len(values)
        total = sum_known(values)
        totals[name] = int(total) if total is not None and name in INTEGER_FIELDS else total
    totals["attempts_observed"] = observed
    return totals


def coverage_of(states: list[str]) -> str:
    """``complete`` only when every state is complete, ``unknown`` when none is known, else ``partial``."""
    if not states or all(s == UsageCompleteness.UNKNOWN for s in states):
        return UsageCompleteness.UNKNOWN
    if all(s == UsageCompleteness.COMPLETE for s in states):
        return UsageCompleteness.COMPLETE
    return UsageCompleteness.PARTIAL


def usage_coverage(attempts: list[RunAttempt], history: str | None) -> str:
    """Usage coverage across attempts plus a legacy summary's coverage (``None`` without one).

    A legacy summary is never complete: older code overwrote it per turn, so
    it is ``partial`` when it recorded any usage and ``unknown`` when it did not.
    """
    states = [a.usage_completeness or UsageCompleteness.UNKNOWN for a in attempts]
    if history is not None:
        states.append(history)
    return coverage_of(states)


def _terminal_reported_credits(attempt: RunAttempt) -> bool:
    """For a terminal result completed from snapshots: whether the credits came from the terminal result."""
    stored = _load_snapshot(attempt)
    filled = stored.get(FILLED_KEY)
    if isinstance(filled, list):
        return "factory_credits" not in filled
    # Rows recorded before the filled categories were kept: the credits can only
    # have come from the terminal result when no attributable snapshot had any.
    attributable = attributable_snapshot_usage(attempt)
    return attributable is None or "factory_credits" not in attributable[0]


def attempt_credit_state(attempt: RunAttempt) -> str | None:
    """Coverage of this attempt's Factory credits alone, or ``None`` when it provably never started.

    Generic usage completeness says nothing about credits: a terminal result
    may report tokens but no credits. Only a terminal credit observation
    (including zero) is complete; snapshot-derived credits are partial; a
    started attempt without observed credits is unknown.
    """
    if attempt.usage_source == UsageSource.NOT_STARTED:
        return None
    if attempt.factory_credits is None:
        return UsageCompleteness.UNKNOWN
    if attempt.usage_source == UsageSource.TERMINAL_RESULT:
        return UsageCompleteness.COMPLETE
    if attempt.usage_source == UsageSource.TERMINAL_WITH_SNAPSHOT:
        return UsageCompleteness.COMPLETE if _terminal_reported_credits(attempt) else UsageCompleteness.PARTIAL
    if attempt.usage_source in UsageSource.SNAPSHOT:
        return UsageCompleteness.PARTIAL
    return (
        UsageCompleteness.COMPLETE
        if attempt.usage_completeness == UsageCompleteness.COMPLETE
        else UsageCompleteness.PARTIAL
    )


def legacy_credit_state(credits: float | None) -> str:
    """A legacy per-task summary was overwritten per turn: known credits are at best partial."""
    return UsageCompleteness.PARTIAL if credits is not None else UsageCompleteness.UNKNOWN


def credit_coverage(attempts: list[RunAttempt], history_credits: list[float | None]) -> dict[str, Any]:
    """Credit coverage across attempts and legacy summaries, with how many entries fell in each state.

    A known subtotal next to any unknown entry is ``partial``; only unknown
    entries, or nothing at all, is ``unknown`` (never an observed zero).
    """
    states: list[str] = []
    exempt = 0
    for attempt in attempts:
        state = attempt_credit_state(attempt)
        if state is None:
            exempt += 1
        else:
            states.append(state)
    attempt_states = list(states)
    legacy = [legacy_credit_state(credits) for credits in history_credits]
    states += legacy
    return {
        "coverage": coverage_of(states),
        "counts": {
            "observed": attempt_states.count(UsageCompleteness.COMPLETE),
            "partial": attempt_states.count(UsageCompleteness.PARTIAL),
            "unknown": attempt_states.count(UsageCompleteness.UNKNOWN),
            "exempt_not_started": exempt,
            "legacy_known": legacy.count(UsageCompleteness.PARTIAL),
            "legacy_unknown": legacy.count(UsageCompleteness.UNKNOWN),
        },
    }


def usd_summary(attempts: list[RunAttempt], *, has_history: bool = False) -> dict[str, Any]:
    """Dollar facts across attempts. A known-only subtotal is never presented as the complete cost."""
    # Blocked-before-spawn attempts used nothing, so they have no cost to know.
    attempts = [a for a in attempts if a.usage_source != UsageSource.NOT_STARTED]
    reconciled = [a for a in attempts if a.cost_status == CostStatus.RECONCILED]
    estimated = [a for a in attempts if a.cost_status == CostStatus.ESTIMATED]
    unknown = [a for a in attempts if a.cost_status == CostStatus.UNKNOWN]
    states = [UsageCompleteness.UNKNOWN] * len(unknown) + [UsageCompleteness.COMPLETE] * len(reconciled)
    # An estimate only covers the credits that were observed.
    states += [
        UsageCompleteness.COMPLETE if a.usage_completeness == UsageCompleteness.COMPLETE else UsageCompleteness.PARTIAL
        for a in estimated
    ]
    if has_history:
        states.append(UsageCompleteness.UNKNOWN)
    coverage = coverage_of(states)
    reconciled_subtotal = sum_known(a.reconciled_usd for a in reconciled)
    estimated_subtotal = sum_known(a.estimated_usd for a in estimated)
    known = sum_known([reconciled_subtotal, estimated_subtotal])
    return {
        "coverage": coverage,
        "billed_usd": None,
        "attempts_unknown": len(unknown),
        "attempts_estimated": len(estimated),
        "attempts_reconciled": len(reconciled),
        "reconciled_usd_subtotal": reconciled_subtotal,
        # Estimates of attempts that also have a reconciled value are superseded, not added.
        "estimated_usd_subtotal": estimated_subtotal,
        "estimates_superseded_by_reconciliation": sum(1 for a in reconciled if a.estimated_usd is not None),
        "known_usd_subtotal": known,
        "total_usd": known if coverage == UsageCompleteness.COMPLETE else None,
        "estimate_sources": sorted({a.estimate_source for a in estimated if a.estimate_source}),
        "reconciliation_sources": sorted({a.reconciliation_source for a in reconciled if a.reconciliation_source}),
    }


__all__ = [
    "ATTRIBUTION_LIMITS",
    "CostInputError",
    "apply_configured_estimate",
    "apply_snapshot_usage",
    "attempt_credit_state",
    "attributable_snapshot_usage",
    "clean_source",
    "clean_usage",
    "configured_estimate_rate",
    "coverage_of",
    "credit_coverage",
    "finalize_usage",
    "finite_number",
    "record_snapshot",
    "record_terminal_usage",
    "set_estimate",
    "set_reconciliation",
    "sum_known",
    "usage_coverage",
    "usage_totals",
    "usd_summary",
]
