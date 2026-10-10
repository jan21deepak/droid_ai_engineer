"""Opt-in product analytics (PostHog).

Reports anonymous usage events (which features operators actually use) when
``POSTHOG_KEY`` is configured; otherwise every call is a no-op. Event capture
is fire-and-forget: analytics failures never affect the product flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.tracing import current_request_id

logger = logging.getLogger("app.analytics")

CAPTURE_TIMEOUT_SECONDS = 2.0


async def capture(event: str, properties: dict[str, Any] | None = None) -> bool:
    """Send an analytics event when PostHog is configured. Returns True when sent."""
    settings = get_settings()
    if not settings.posthog_key.strip():
        return False

    payload = {
        "api_key": settings.posthog_key,
        "event": event,
        "distinct_id": current_request_id(),
        "properties": {"source": "droid-forge", **(properties or {})},
    }
    try:
        async with httpx.AsyncClient(timeout=CAPTURE_TIMEOUT_SECONDS) as client:
            resp = await client.post(f"{settings.posthog_host}/capture/", json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:
        # Analytics must never break product flow; log and move on.
        logger.warning("analytics.capture_failed | error=%s event=%s", exc, event)
        return False


# Keep references to in-flight captures so the GC can't drop them mid-request.
_pending_captures: set[asyncio.Task[bool]] = set()


def fire_and_forget(event: str, properties: dict[str, Any] | None = None) -> None:
    """Schedule an analytics capture on the running loop without blocking."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(capture(event, properties))
    _pending_captures.add(task)
    task.add_done_callback(_pending_captures.discard)
