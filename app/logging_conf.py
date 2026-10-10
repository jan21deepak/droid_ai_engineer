"""Structured, operator-friendly logging configuration.

Logs are single-line ``key=value`` events with ISO-8601 Singapore timestamps.
Two filters run on every record:

- ``RequestIDFilter`` — stamps the current request id (see ``app.tracing``)
  onto each record so log lines from the same HTTP request are correlatable.
- ``ScrubbingFilter`` — masks token-shaped secrets (GitHub PATs, bearer tokens,
  ``key=value`` pairs for token/key/secret fields) so credentials never reach
  log files.
"""

import logging
import re
import sys
from datetime import datetime
from typing import Any

from app.timeutil import SGT

# Token shapes seen in this system: GitHub PATs (classic + fine-grained),
# Factory keys, generic bearer/jwt-ish strings.
SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+"),
]
# key=value / key: value contexts whose value is always sensitive. Matches
# compound field names like GITHUB_TOKEN or api_key but not "tokens=1234".
SECRET_KV = re.compile(
    r"(?i)(?<![\w.-])((?:[\w.-]+_)*(?:api_?key|token|secret|password)(?:_[\w-]+)*)(\s*[=:]\s*)(\"?)([^\s\",;]+)"
)

MASK = "[REDACTED]"


def scrub(value: str) -> str:
    """Mask token-shaped secrets and sensitive key=value pairs in free text."""
    result = value
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(MASK, result)
    return SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", result)


def scrub_ctx(ctx: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive-looking values inside structured log context fields."""
    cleaned: dict[str, Any] = {}
    for key, value in ctx.items():
        if any(word in key.lower() for word in ("token", "key", "secret", "password")):
            cleaned[key] = MASK
        elif isinstance(value, str):
            cleaned[key] = scrub(value)
        else:
            cleaned[key] = value
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
                record.args = {
                    key: (scrub(value) if isinstance(value, str) else value) for key, value in record.args.items()
                }
            else:
                record.args = tuple(scrub(arg) if isinstance(arg, str) else arg for arg in record.args)
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
            base = f"{base}\n{self.formatException(record.exc_info)}"
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
