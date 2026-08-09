"""Time helpers. All user-facing timestamps are rendered in Singapore time.

Singapore has observed a constant UTC+08:00 offset since 1982 and no DST, so a
fixed offset is used rather than a tz database lookup.
"""

from datetime import date, datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8), "SGT")


def as_utc(value: datetime | None) -> datetime | None:
    """Treat naive datetimes as UTC — SQLite drops tzinfo on read."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def to_sgt(value: datetime | None) -> datetime | None:
    aware = as_utc(value)
    return aware.astimezone(SGT) if aware else None


def iso_sgt(value: datetime | None) -> str | None:
    """ISO-8601 string carrying the +08:00 offset so clients parse it correctly."""
    converted = to_sgt(value)
    return converted.isoformat() if converted else None


def format_sgt(value: datetime | None) -> str | None:
    converted = to_sgt(value)
    return converted.strftime("%d %b %Y %H:%M SGT") if converted else None


def sgt_date(value: datetime | None) -> date | None:
    converted = to_sgt(value)
    return converted.date() if converted else None


def now_sgt() -> datetime:
    return datetime.now(SGT)
