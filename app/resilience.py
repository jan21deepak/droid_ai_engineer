"""Retry with exponential backoff for calls to external services.

Used by the GitHub client so transient failures (rate limits, network blips,
5xx) don't fail an otherwise healthy run. Kept dependency-free and testable.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryError(RuntimeError):
    """All retry attempts were exhausted."""

    def __init__(self, attempts: int, last_error: Exception):
        super().__init__(f"failed after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


async def retry_with_backoff[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retry_on: tuple[type[Exception], ...] = (httpx.TransportError, httpx.HTTPStatusError),
) -> T:
    """Run ``fn`` with exponential backoff on transport errors and 429/5xx.

    Non-retryable HTTP errors (4xx other than 429) raise immediately.
    ``sleep`` is injectable so tests stay deterministic.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(exc, httpx.HTTPStatusError) and status not in RETRYABLE_STATUS:
                raise
            if attempt == attempts:
                raise RetryError(attempts, exc) from exc
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            # Full jitter: spread retries to avoid hammering a recovering service.
            await sleep(delay * random.random())
    raise RuntimeError(f"unreachable: retry loop exhausted (last_error={last_error!r})")


def is_retryable_status(status: int) -> bool:
    """Whether an HTTP status should be retried (429/5xx)."""
    return status in RETRYABLE_STATUS


def describe_call(fn: Callable[..., Any]) -> str:
    """Human-readable name for logging retry outcomes."""
    return getattr(fn, "__name__", repr(fn))
