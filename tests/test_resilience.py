"""Tests for retry-with-backoff (app/resilience.py)."""

import httpx
import pytest

from app.resilience import RetryError, retry_with_backoff


def http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class TestRetryWithBackoff:
    async def test_returns_first_success_without_sleeping(self):
        sleeps: list[float] = []

        async def ok():
            return "value"

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        assert await retry_with_backoff(ok, sleep=fake_sleep) == "value"
        assert sleeps == []

    async def test_retries_then_succeeds(self):
        calls = 0
        sleeps: list[float] = []

        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectError("boom")
            return "recovered"

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        assert await retry_with_backoff(flaky, sleep=fake_sleep) == "recovered"
        assert calls == 3
        assert len(sleeps) == 2
        # Exponential backoff: each delay bound shrinks toward the base cap.
        assert sleeps[1] <= 1.0

    async def test_non_retryable_status_raises_immediately(self):
        attempts = 0

        async def forbidden():
            nonlocal attempts
            attempts += 1
            raise http_status_error(403)

        with pytest.raises(httpx.HTTPStatusError):
            await retry_with_backoff(forbidden, sleep=None)  # type: ignore[arg-type]
        assert attempts == 1

    async def test_raises_retry_error_after_exhausting_attempts(self):
        attempts = 0

        async def always_500():
            nonlocal attempts
            attempts += 1
            raise http_status_error(503)

        async def fake_sleep(delay: float) -> None:
            pass

        with pytest.raises(RetryError):
            await retry_with_backoff(always_500, attempts=3, sleep=fake_sleep)
        assert attempts == 3

    async def test_unexpected_exceptions_propagate(self):
        async def broken():
            raise ValueError("not a transport problem")

        with pytest.raises(ValueError):
            await retry_with_backoff(broken, sleep=None)  # type: ignore[arg-type]
