"""Structured, operator-friendly logging configuration.

Logs are single-line ``key=value`` events with ISO-8601 Singapore timestamps.
Two filters run on every record:

- ``RequestIDFilter`` — stamps the current request id (see ``app.tracing``)
  onto each record so log lines from the same HTTP request are correlatable.
- ``ScrubbingFilter`` — masks configured credential values, token-shaped
  secrets (GitHub PATs, bearer tokens, ``key=value`` pairs for token/key/secret
  fields) and nested structured context so credentials never reach log files.
"""

import logging
import sys
from datetime import datetime
from typing import Any

from app.redaction import MASK, is_sensitive_key, known_secret_values, redact, redact_text
from app.timeutil import SGT

__all__ = ["MASK", "log_event", "scrub", "scrub_ctx", "setup_logging"]


def scrub(value: str) -> str:
    """Mask known credentials, token-shaped secrets and sensitive key=value pairs in free text."""
    return redact_text(value)


def scrub_ctx(ctx: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive-looking values inside (possibly nested) structured log context fields."""
    secrets = known_secret_values()
    cleaned: dict[str, Any] = {}
    for key, value in ctx.items():
        if is_sensitive_key(key):
            cleaned[key] = MASK
        else:
            cleaned[key] = redact(value, secrets=secrets)
    return cleaned


class RequestIDFilter(logging.Filter):
    """Attach the current request id to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        from app.tracing import current_request_id

        record.request_id = current_request_id()
        return True


class ScrubbingFilter(logging.Filter):
    """Strip secrets from the rendered message and structured context."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact(record.args)
            else:
                record.args = tuple(redact(arg) for arg in record.args)
        record.msg = scrub(str(record.msg))
        ctx = getattr(record, "ctx", None)
        if ctx:
            record.ctx = scrub_ctx(ctx)
        return True


class StructuredFormatter(logging.Formatter):
    """Single-line key=value formatter with ISO-8601 Singapore timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=SGT).isoformat(timespec="milliseconds")
        rid = getattr(record, "request_id", "-")
        base = f"{ts} | {record.levelname:<8} | {record.name} | [{rid}] {record.getMessage()}"
        extras = getattr(record, "ctx", None)
        if extras:
            kv = " ".join(f"{k}={v}" for k, v in extras.items())
            base = f"{base} | {kv}"
        if record.exc_info:
            base = f"{base}\n{scrub(self.formatException(record.exc_info))}"
        return base


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    handler.addFilter(RequestIDFilter())
    handler.addFilter(ScrubbingFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, event: str, **ctx: Any) -> None:
    """Log a named event with structured context fields."""
    logger.log(level, event, extra={"ctx": ctx})
