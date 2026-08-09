"""Structured, operator-friendly logging configuration."""

import logging
import sys
from datetime import datetime

from app.timeutil import SGT


class StructuredFormatter(logging.Formatter):
    """Single-line key=value formatter with ISO-8601 Singapore timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=SGT).isoformat(timespec="milliseconds")
        base = f"{ts} | {record.levelname:<8} | {record.name} | {record.getMessage()}"
        extras = getattr(record, "ctx", None)
        if extras:
            kv = " ".join(f"{k}={v}" for k, v in extras.items())
            base = f"{base} | {kv}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, event: str, **ctx) -> None:
    """Log a named event with structured context fields."""
    logger.log(level, event, extra={"ctx": ctx})
