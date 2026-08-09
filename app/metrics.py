"""Aggregate metrics computed dynamically from SQLite."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ReviewTask, Task, TaskStatus
from app.timeutil import now_sgt, sgt_date


def _round(value: float | None, digits: int = 2) -> float:
    if value is None:
        return 0.0
    return round(float(value), digits)


def task_cost_usd(task: Task, settings=None) -> float:
    """Return cost derived only from Devin API-reported ACU usage."""
    settings = settings or get_settings()
    if task.acus_consumed is not None:
        return float(task.acus_consumed) * settings.devin_acu_usd
    return 0.0


def _bucket_stats(durations: list[float], counts: dict[str, int]) -> dict:
    running = counts.get(TaskStatus.RUNNING, 0) + counts.get(TaskStatus.QUEUED, 0)
    completed = counts.get(TaskStatus.COMPLETED, 0)
    failed = counts.get(TaskStatus.FAILED, 0)
    total = sum(counts.values())
    finished = completed + failed
    success_rate = round(completed / finished * 100, 2) if finished else 0.0
    avg_secs = (sum(durations) / len(durations)) if durations else 0.0
    total_secs = sum(durations)
    return {
        "total": total,
        "running": running,
        "completed": completed,
        "failed": failed,
        "success_rate": success_rate,
        "average_runtime_seconds": _round(avg_secs),
        "average_runtime_minutes": _round(avg_secs / 60.0),
        "total_runtime_seconds": _round(total_secs),
        "total_runtime_minutes": _round(total_secs / 60.0),
    }


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _engineering_kpis(
    all_fixes: list[Task],
    completed_fixes: list[Task],
    finished_fixes: list[Task],
    review_stats: dict,
) -> dict:
    """Delivery KPIs computed only from observed issue-fix and review records.

    Every value here is a direct count or average over rows in the database.
    Nothing is extrapolated, projected, or rate-normalized.
    """
    cycle_hours: list[float] = []
    for task in completed_fixes:
        # Average active Devin working time (idle / human waits already stripped
        # from duration_seconds). Prefer that over wall-clock assign→done, which
        # inflates badly when sessions sit waiting for a human.
        if task.duration_seconds is not None:
            cycle_hours.append(float(task.duration_seconds) / 3600.0)
            continue
        start = _aware(task.created_at)
        end = _aware(task.completed_at)
        if start and end and end >= start:
            cycle_hours.append((end - start).total_seconds() / 3600.0)

    # A PR counts as delivered once it exists, regardless of whether Devin's
    # session later finished — sessions often stay open waiting for the user.
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    pr_first_seen: dict[str, datetime | None] = {}
    pr_state: dict[str, str | None] = {}
    for task in all_fixes:
        url = task.pull_request_url
        if not url:
            continue
        stamp = _aware(task.completed_at) or _aware(task.created_at)
        current = pr_first_seen.get(url)
        if url not in pr_first_seen or (stamp and current and stamp < current):
            pr_first_seen[url] = stamp
        if task.pr_state == "merged" or url not in pr_state:
            pr_state[url] = task.pr_state
    prs_last_7_days = sum(
        1 for stamp in pr_first_seen.values() if stamp and stamp >= cutoff
    )
    prs = list(pr_first_seen)

    # Merge rate is measured against every PR Devin opened, not just the ones
    # that happen to have a review row — otherwise unreviewed PRs are invisible
    # and the rate reads 100% while PRs sit unmerged.
    prs_merged = sum(1 for state in pr_state.values() if state == "merged")
    prs_closed = sum(1 for state in pr_state.values() if state == "closed")
    prs_open = len(prs) - prs_merged - prs_closed
    merge_rate = round(prs_merged / len(prs) * 100, 2) if prs else 0.0

    # Counted across every finished Devin run, fixes and reviews alike, so this
    # agrees with the Failed status card. A fix-only rate reads 0% while failed
    # reviews are on screen.
    fixes_finished = len(finished_fixes)
    fixes_failed = sum(1 for t in finished_fixes if t.status == TaskStatus.FAILED)
    reviews_failed = review_stats.get("failed", 0)
    reviews_finished = review_stats.get("completed", 0) + reviews_failed
    runs_failed = fixes_failed + reviews_failed
    runs_finished = fixes_finished + reviews_finished
    change_failure = round(runs_failed / runs_finished * 100, 2) if runs_finished else 0.0

    return {
        "pr_cycle_time_hours": _round(
            sum(cycle_hours) / len(cycle_hours) if cycle_hours else 0.0
        ),
        "pr_cycle_time_sample": len(cycle_hours),
        "prs_last_7_days": prs_last_7_days,
        "prs_total": len(prs),
        "prs_merged": prs_merged,
        "prs_open": prs_open,
        "prs_closed": prs_closed,
        "merge_rate_percent": merge_rate,
        "reviews_merged": review_stats.get("merged", 0),
        "reviews_completed": review_stats.get("completed", 0),
        "change_failure_rate_percent": change_failure,
        "fixes_failed": fixes_failed,
        "fixes_finished": fixes_finished,
        "reviews_failed": reviews_failed,
        "reviews_finished": reviews_finished,
        "runs_failed": runs_failed,
        "runs_finished": runs_finished,
    }


def compute_metrics(session: Session) -> dict:
    settings = get_settings()
    # Fix metrics: Devin sessions dispatched for repository issue fixes.
    issue_tasks = session.query(Task).filter(Task.devin_session_id.isnot(None))
    fix_counts = dict(
        issue_tasks.with_entities(Task.status, func.count(Task.id))
        .group_by(Task.status)
        .all()
    )
    fix_durations = [
        float(d)
        for (d,) in (
            issue_tasks.with_entities(Task.duration_seconds)
            .filter(Task.status == TaskStatus.COMPLETED, Task.duration_seconds.isnot(None))
            .all()
        )
        if d is not None
    ]
    # Include failed durations in total runtime when present
    fix_total_durations = [
        float(d)
        for (d,) in (
            issue_tasks.with_entities(Task.duration_seconds)
            .filter(Task.duration_seconds.isnot(None))
            .all()
        )
        if d is not None
    ]
    fix_stats = _bucket_stats(fix_durations, fix_counts)
    fix_stats["total_runtime_seconds"] = _round(sum(fix_total_durations))
    fix_stats["total_runtime_minutes"] = _round(sum(fix_total_durations) / 60.0)

    # Review metrics: Devin Review runs against pull requests (auto-triggered).
    review_q = session.query(ReviewTask)
    review_counts = dict(
        review_q.with_entities(ReviewTask.status, func.count(ReviewTask.id))
        .group_by(ReviewTask.status)
        .all()
    )
    review_durations = [
        float(d)
        for (d,) in (
            review_q.with_entities(ReviewTask.duration_seconds)
            .filter(
                ReviewTask.status == TaskStatus.COMPLETED,
                ReviewTask.duration_seconds.isnot(None),
            )
            .all()
        )
        if d is not None
    ]
    review_total_durations = [
        float(d)
        for (d,) in (
            review_q.with_entities(ReviewTask.duration_seconds)
            .filter(ReviewTask.duration_seconds.isnot(None))
            .all()
        )
        if d is not None
    ]
    review_stats = _bucket_stats(review_durations, review_counts)
    review_stats["total_runtime_seconds"] = _round(sum(review_total_durations))
    review_stats["total_runtime_minutes"] = _round(sum(review_total_durations) / 60.0)
    review_stats["merged"] = (
        review_q.filter(ReviewTask.merged.is_(True)).count()
    )

    all_fixes = issue_tasks.all()
    completed_fixes = [t for t in all_fixes if t.status == TaskStatus.COMPLETED]
    all_finished_fixes = [
        t for t in all_fixes if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
    ]
    all_finished_reviews = (
        review_q.filter(ReviewTask.status.in_((TaskStatus.COMPLETED, TaskStatus.FAILED))).all()
    )

    tokens_used = None
    fix_cost = sum(task_cost_usd(t, settings) for t in all_finished_fixes)
    review_cost = sum(float(r.cost_usd or 0) for r in all_finished_reviews)
    total_acus = sum(float(t.acus_consumed or 0) for t in all_finished_fixes)
    devin_cost = fix_cost + review_cost

    junior_hourly = settings.junior_swe_annual_cost_usd / 1920.0
    fix_completed = fix_stats["completed"]
    review_completed = review_stats["completed"]
    junior_fix_cost = fix_completed * settings.junior_hours_per_issue * junior_hourly
    junior_review_cost = (
        review_completed * settings.junior_hours_per_review * junior_hourly
    )
    junior_cost = junior_fix_cost + junior_review_cost
    productivity_gained = max(junior_cost - devin_cost, 0.0)
    cost_avoidance_pct = (
        round((productivity_gained / junior_cost) * 100, 2) if junior_cost else 0.0
    )

    engineering = _engineering_kpis(
        all_fixes, completed_fixes, all_finished_fixes, review_stats
    )

    # Combined top-line counts for the status cards.
    combined_running = fix_stats["running"] + review_stats["running"]
    combined_completed = fix_completed + review_completed
    combined_failed = fix_stats["failed"] + review_stats["failed"]
    combined_total = fix_stats["total"] + review_stats["total"]
    combined_finished = combined_completed + combined_failed
    combined_success = (
        round(combined_completed / combined_finished * 100, 2) if combined_finished else 0.0
    )

    return {
        "total": combined_total,
        "running": combined_running,
        "completed": combined_completed,
        "failed": combined_failed,
        "success_rate": combined_success,
        # Back-compat aliases = fix-time stats (used by older clients / tests)
        "average_runtime_seconds": fix_stats["average_runtime_seconds"],
        "average_runtime_minutes": fix_stats["average_runtime_minutes"],
        "total_runtime_seconds": fix_stats["total_runtime_seconds"],
        "total_runtime_minutes": fix_stats["total_runtime_minutes"],
        "fix": fix_stats,
        "review": review_stats,
        "engineering": engineering,
        "tokens_used": tokens_used,
        "tokens_source": "not_reported_by_devin_api",
        "total_acus_consumed": _round(total_acus, 4),
        "devin_cost_usd": _round(devin_cost),
        "junior_cost_usd": _round(junior_cost),
        "productivity_gained_usd": _round(productivity_gained),
        "cost_avoidance_percent": cost_avoidance_pct,
        "assumptions": {
            "junior_swe_annual_cost_usd": settings.junior_swe_annual_cost_usd,
            "junior_hourly_usd": _round(junior_hourly),
            "junior_hours_per_issue": settings.junior_hours_per_issue,
            "junior_hours_per_review": settings.junior_hours_per_review,
            "devin_acu_usd": settings.devin_acu_usd,
            "formula": (
                "productivity_gained = "
                "(completed_fixes × junior_hours_per_issue + "
                "completed_reviews × junior_hours_per_review) × junior_hourly "
                "− Devin cost"
            ),
        },
        "completed_with_pr": sum(1 for t in completed_fixes if t.pull_request_url),
        "timezone": "Asia/Singapore (SGT, UTC+08:00)",
        "daily": daily_activity(session),
    }


def daily_activity(session: Session, days: int = 14) -> list[dict]:
    """Per-day activity buckets for the dashboard chart, keyed by SGT calendar day.

    Fixes and reviews are bucketed on the day they finished; a PR is bucketed on
    the day its fix task first reported it.
    """
    today = now_sgt().date()
    window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    buckets = {
        day: {
            "date": day.isoformat(),
            "label": day.strftime("%d %b"),
            "fixes_completed": 0,
            "fixes_failed": 0,
            "reviews_completed": 0,
            "prs_opened": 0,
            "runtime_minutes": 0.0,
        }
        for day in window
    }
    earliest = window[0]

    def bucket_for(value) -> dict | None:
        day = sgt_date(value)
        if day is None or day < earliest or day > today:
            return None
        return buckets[day]

    fixes = session.query(Task).filter(Task.devin_session_id.isnot(None)).all()
    seen_prs: set[str] = set()
    for task in fixes:
        finished = bucket_for(task.completed_at)
        if finished:
            if task.status == TaskStatus.COMPLETED:
                finished["fixes_completed"] += 1
            elif task.status == TaskStatus.FAILED:
                finished["fixes_failed"] += 1
            if task.duration_seconds:
                finished["runtime_minutes"] += float(task.duration_seconds) / 60.0
        if task.pull_request_url and task.pull_request_url not in seen_prs:
            seen_prs.add(task.pull_request_url)
            opened = bucket_for(task.completed_at or task.created_at)
            if opened:
                opened["prs_opened"] += 1

    for review in session.query(ReviewTask).all():
        # Reviews discovered already-finished have no observed completion time;
        # fall back to when Devin started them so they still appear on the chart.
        finished = bucket_for(review.completed_at or review.created_at)
        if not finished:
            continue
        if review.status == TaskStatus.COMPLETED:
            finished["reviews_completed"] += 1
        if review.duration_seconds:
            finished["runtime_minutes"] += float(review.duration_seconds) / 60.0

    series = []
    for day in window:
        bucket = buckets[day]
        bucket["runtime_minutes"] = _round(bucket["runtime_minutes"])
        series.append(bucket)
    return series


def recent_tasks(session: Session, limit: int | None = None) -> list[Task]:
    query = session.query(Task).order_by(Task.created_at.desc())
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def all_activity(session: Session) -> list[dict]:
    """Every fix and review task, newest first. No artificial trim."""
    fixes = [t.to_dict() for t in recent_tasks(session)]
    reviews = [
        r.to_dict()
        for r in session.query(ReviewTask).order_by(ReviewTask.created_at.desc()).all()
    ]
    combined = fixes + reviews
    combined.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return combined


def recent_activity(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 10,
) -> dict:
    """Paginated fix + review activity for the dashboard table.

    Returns every activity item across pages — nothing is discarded, only
    sliced for display. ``page_size`` is capped at 100.
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 100))
    items = all_activity(session)
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = min(page, total_pages)
    offset = (page - 1) * page_size
    return {
        "tasks": items[offset : offset + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }
