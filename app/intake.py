"""Signed GitHub webhook intake, independent of operator authentication."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.github import IssueEvent, parse_issue_event, should_trigger, verify_signature
from app.logging_conf import log_event
from app.policy import approved_registered_repository
from app.tasks import TaskCreateError, create_and_dispatch_task

logger = logging.getLogger("app.api")
router = APIRouter()

MAX_WEBHOOK_BYTES = 2 * 1024 * 1024
MAX_DELIVERY_ID_CHARS = 128


def _rejected(status: int, detail: str, reason: str, **ctx: Any) -> JSONResponse:
    log_event(logger, logging.WARNING, "webhook.rejected", reason=reason, status=status, **ctx)
    return JSONResponse(status_code=status, content={"detail": detail})


async def _verified_payload(request: Request, settings: Settings) -> dict[str, Any] | JSONResponse:
    """Authenticate and decode the delivery, or return the rejection response."""
    if not settings.github_webhook_secret:
        return _rejected(503, "webhook intake disabled: signing secret not configured", "secret_unconfigured")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        return _rejected(413, "payload too large", "payload_too_large")
    if not verify_signature(settings.github_webhook_secret, body, request.headers.get("X-Hub-Signature-256")):
        log_event(
            logger, logging.WARNING, "webhook.signature_invalid", github_event=request.headers.get("X-GitHub-Event", "")
        )
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _rejected(422, "invalid JSON payload", "invalid_json")
    if not isinstance(payload, dict):
        return _rejected(422, "invalid JSON payload", "non_object_json")
    return payload


def _issue_event(payload: dict[str, Any]) -> IssueEvent | None:
    try:
        event = parse_issue_event(payload)
    except (AttributeError, TypeError, ValueError):
        return None
    if not event.repository or not isinstance(event.repository, str):
        return None
    return event


@router.post("/webhook", response_model=None)
async def webhook(request: Request) -> JSONResponse | dict[str, Any]:
    """GitHub `issues` webhook; requires its own HMAC (`X-Hub-Signature-256`), not operator auth."""
    settings = get_settings()
    event_type = request.headers.get("X-GitHub-Event", "")
    payload = await _verified_payload(request, settings)
    if isinstance(payload, JSONResponse):
        return payload

    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()[:MAX_DELIVERY_ID_CHARS]
    log_event(logger, logging.INFO, "webhook.received", github_event=event_type, action=payload.get("action"))

    if event_type == "ping":
        return {"detail": "pong"}
    if event_type != "issues":
        return {"detail": f"ignored event type: {event_type or 'unknown'}"}

    event = _issue_event(payload)
    if event is None:
        return _rejected(422, "malformed issues payload", "malformed_issue")

    approved = approved_registered_repository(event.repository, settings)
    if approved is None:
        return _rejected(
            403,
            "repository is not registered and approved for intake",
            "repository_not_approved",
            repo=event.repository[:200],
        )

    if not should_trigger(event, settings.trigger_label):
        log_event(
            logger,
            logging.INFO,
            "webhook.ignored",
            repo=approved.full_name,
            issue=event.issue_number,
            action=event.action,
            reason=f"no trigger (label '{settings.trigger_label}' required)",
        )
        return {"detail": "ignored: trigger conditions not met"}

    try:
        result = await create_and_dispatch_task(
            repository=approved.full_name,
            repository_url=approved.url,
            issue_number=event.issue_number,
            issue_title=event.issue_title,
            issue_body=event.issue_body,
            labels=event.labels,
            delivery_id=delivery_id or None,
        )
    except TaskCreateError as exc:
        if exc.status_code == 200:
            return {"detail": exc.message, "task_id": exc.task_id}
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "task_id": exc.task_id},
        )

    return JSONResponse(status_code=202, content={"detail": "accepted", **result})
