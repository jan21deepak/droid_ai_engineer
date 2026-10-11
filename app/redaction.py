"""Credential redaction applied before anything is persisted, logged or displayed.

Two layers run on every string:

- **Known values**: the exact configured credentials (GitHub token, webhook
  secret, operator token, Factory key, ...), plus encodings an error message can
  plausibly contain (the Git ``Authorization: basic`` form and URL-quoting).
  Any environment variable whose name marks it as sensitive is included too.
- **Shapes**: token-shaped strings (PATs, ``sk-`` keys, JWTs), ``key=value``
  pairs for sensitive field names, URL userinfo and auth headers.

``redact`` walks nested dicts/lists so structured error payloads are cleaned
as well as flat strings.
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any
from urllib.parse import quote

MASK = "[REDACTED]"
MAX_ERROR_CHARS = 2000

# Shorter values are too likely to collide with ordinary words.
_MIN_SECRET_LENGTH = 8
_SENSITIVE_NAME_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "PRIVATE_KEY", "CREDENTIAL", "DSN")
_SENSITIVE_KEY_WORDS = ("token", "key", "secret", "password", "credential", "authorization", "dsn")

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
_URL_USERINFO = re.compile(r"(?i)\b((?:https?|ssh|git)://)[^/\s@]+@")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*:\s*(?:basic|bearer|token)\s+)\S+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def git_basic_auth_value(token: str) -> str:
    """The base64 credential GitHub accepts for HTTPS Git (``x-access-token`` user)."""
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _configured_secrets() -> list[str]:
    from app.config import get_settings

    settings = get_settings()
    return [
        settings.github_token,
        settings.github_webhook_secret,
        settings.forge_operator_token,
        settings.factory_api_key,
        settings.posthog_key,
        settings.sentry_dsn,
    ]


def _sensitive_environment_values() -> list[str]:
    values = []
    for name, value in os.environ.items():
        upper = name.upper()
        if any(marker in upper for marker in _SENSITIVE_NAME_MARKERS):
            values.append(value)
    return values


def known_secret_values() -> list[str]:
    """Every credential value (and derived encoding) that must never be shown, longest first."""
    raw = {value.strip() for value in [*_configured_secrets(), *_sensitive_environment_values()] if value}
    expanded: set[str] = set()
    for value in raw:
        if len(value) < _MIN_SECRET_LENGTH:
            continue
        expanded.add(value)
        expanded.add(git_basic_auth_value(value))
        quoted = quote(value, safe="")
        if quoted != value:
            expanded.add(quoted)
    return sorted(expanded, key=len, reverse=True)


def redact_text(value: str, *, secrets: list[str] | None = None) -> str:
    """Mask known credentials and credential-shaped substrings in free text."""
    if not value:
        return value
    result = value
    for secret in secrets if secrets is not None else known_secret_values():
        if secret in result:
            result = result.replace(secret, MASK)
    result = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{MASK}@", result)
    result = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{MASK}", result)
    result = _BEARER.sub(lambda m: f"{m.group(1)}{MASK}", result)
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(MASK, result)
    return SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", result)


def is_sensitive_key(key: object) -> bool:
    lowered = str(key).lower()
    if lowered in {"tokens", "estimated_tokens", "input_tokens", "output_tokens"} or lowered.endswith("_tokens"):
        return False
    return any(word in lowered for word in _SENSITIVE_KEY_WORDS)


def redact(value: Any, *, secrets: list[str] | None = None) -> Any:
    """Recursively redact strings inside dicts, lists, tuples and exceptions."""
    known = secrets if secrets is not None else known_secret_values()
    if isinstance(value, str):
        return redact_text(value, secrets=known)
    if isinstance(value, BaseException):
        return redact_text(str(value), secrets=known)
    if isinstance(value, dict):
        return {
            key: (MASK if is_sensitive_key(key) and isinstance(item, str) and item else redact(item, secrets=known))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets=known) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, secrets=known) for item in value)
    return value


def safe_error(value: object, *, limit: int = MAX_ERROR_CHARS) -> str:
    """Redacted, bounded text for an error stored on a row or returned by the API."""
    text = redact_text(str(value))
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
