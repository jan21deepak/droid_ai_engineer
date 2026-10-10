"""Optional error tracking (Sentry).

Enabled only when ``SENTRY_DSN`` is configured; the SDK is imported lazily so
the dependency stays optional for local development. Errors captured by Sentry
carry the current Droid request id for correlation with structured logs.
"""

from __future__ import annotations

import logging

from app.tracing import current_request_id

logger = logging.getLogger("app.error_tracking")


def init_error_tracking(dsn: str, environment: str = "local") -> bool:
    """Initialize Sentry when a DSN is configured. Returns True when active."""
    if not dsn.strip():
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("error_tracking.sentry_missing_install")
        return False

    def before_send(event: dict, hint: dict) -> dict:  # type: ignore[type-arg]
        rid = current_request_id()
        if rid != "-":
            event.setdefault("tags", {})["droid_request_id"] = rid
        return event

    sentry_sdk.init(dsn=dsn, environment=environment, before_send=before_send)
    logger.info("error_tracking.initialized", extra={"ctx": {"environment": environment}})
    return True
