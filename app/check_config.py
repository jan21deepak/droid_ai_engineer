"""Configured local acceptance checks (``FORGE_ACCEPTANCE_CHECKS``).

The list is operator configuration, read at call time; it is never accepted
over HTTP, so supplying data to the API can never choose what runs. Each
check is an argv list executed without a shell. A missing or invalid list is
reported as such and blocks approval: no checks is not the same as passing.
"""

from __future__ import annotations

import json
import math
import shlex
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.redaction import redact_text

DEFAULT_CHECK_TIMEOUT_SECONDS = 900.0
MAX_CHECK_TIMEOUT_SECONDS = 7200.0
MAX_CHECKS = 20
MAX_NAME_CHARS = 128


@dataclass(frozen=True)
class CheckSpec:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float

    @property
    def display_command(self) -> str:
        return redact_text(shlex.join(self.argv))


@dataclass(frozen=True)
class CheckConfiguration:
    checks: tuple[CheckSpec, ...]
    error: str | None = None

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.checks]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "FORGE_ACCEPTANCE_CHECKS",
            "error": self.error,
            "checks": [
                {"name": c.name, "command": c.display_command, "timeout_seconds": c.timeout_seconds, "required": True}
                for c in self.checks
            ],
        }


class _ConfigError(ValueError):
    pass


def _timeout(value: Any, name: str) -> float:
    if value is None:
        return DEFAULT_CHECK_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _ConfigError(f"check {name!r}: timeout_seconds must be a number")
    if not 0 < value <= MAX_CHECK_TIMEOUT_SECONDS:
        raise _ConfigError(f"check {name!r}: timeout_seconds must be in (0, {MAX_CHECK_TIMEOUT_SECONDS:g}]")
    return float(value)


def _spec(item: Any, seen: set[str]) -> CheckSpec:
    if not isinstance(item, dict):
        raise _ConfigError("each check must be an object with name and argv")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        raise _ConfigError(f"each check needs a name of 1-{MAX_NAME_CHARS} characters")
    name = name.strip()
    if name in seen:
        raise _ConfigError(f"duplicate check name {name!r}")
    argv = item.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise _ConfigError(f"check {name!r}: argv must be a non-empty list of non-empty strings")
    seen.add(name)
    return CheckSpec(name=name, argv=tuple(argv), timeout_seconds=_timeout(item.get("timeout_seconds"), name))


def configured_checks(settings: Settings | None = None) -> CheckConfiguration:
    raw = ((settings or get_settings()).forge_acceptance_checks or "").strip()
    if not raw:
        return CheckConfiguration(())
    try:
        data = json.loads(raw)
    except ValueError:
        return CheckConfiguration((), "FORGE_ACCEPTANCE_CHECKS is not valid JSON")
    try:
        if not isinstance(data, list) or not data:
            raise _ConfigError("FORGE_ACCEPTANCE_CHECKS must be a non-empty JSON list")
        if len(data) > MAX_CHECKS:
            raise _ConfigError(f"at most {MAX_CHECKS} acceptance checks are supported")
        seen: set[str] = set()
        return CheckConfiguration(tuple(_spec(item, seen) for item in data))
    except _ConfigError as exc:
        return CheckConfiguration((), redact_text(str(exc)))


__all__ = ["CheckConfiguration", "CheckSpec", "configured_checks"]
