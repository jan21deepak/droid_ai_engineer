"""Operator login, logout and session introspection routes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import (
    SESSION_COOKIE,
    SESSIONS,
    cookie_secure,
    csrf_valid,
    expires_at_iso,
    operator_context,
    operator_credential_problem,
    safe_next,
    same_origin,
    verify_operator_token,
)
from app.config import ACCESS_MODE_LOCAL_DEV, effective_access_mode, get_settings
from app.logging_conf import log_event

logger = logging.getLogger("app.auth")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

MAX_FORM_BYTES = 8192


async def _read_form(request: Request) -> dict[str, str] | None:
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return None
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _login_page(request: Request, *, status: int, error: str = "", next_url: str = "/dashboard") -> Response:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "next_url": safe_next(next_url)},
        status_code=status,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/dashboard") -> Response:
    """Operator sign-in page (public)."""
    settings = get_settings()
    if effective_access_mode(settings) == ACCESS_MODE_LOCAL_DEV:
        return RedirectResponse(safe_next(next), status_code=303)
    if operator_credential_problem(settings) is not None:
        return _login_page(request, status=503, error="Operator access is not configured on this server.")
    if SESSIONS.get(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return _login_page(request, status=200, next_url=next)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    """Exchange the operator credential for a session cookie (form-encoded `token`, `next`)."""
    settings = get_settings()
    if effective_access_mode(settings) == ACCESS_MODE_LOCAL_DEV:
        return RedirectResponse("/dashboard", status_code=303)
    if operator_credential_problem(settings) is not None:
        return _login_page(request, status=503, error="Operator access is not configured on this server.")
    if same_origin(request) is not True:
        log_event(logger, logging.WARNING, "auth.login_rejected", reason="origin_mismatch")
        return JSONResponse(status_code=403, content={"detail": "cross-origin request rejected"})

    form = await _read_form(request)
    if form is None:
        return JSONResponse(status_code=413, content={"detail": "login form too large"})
    next_url = safe_next(form.get("next"))
    if not verify_operator_token(form.get("token", ""), settings):
        log_event(logger, logging.WARNING, "auth.login_failed")
        return _login_page(request, status=401, error="Invalid operator token.", next_url=next_url)

    # Rotate on login so a pre-planted cookie can never be promoted (fixation).
    SESSIONS.revoke(request.cookies.get(SESSION_COOKIE))
    ttl = settings.forge_session_ttl_seconds
    raw, _ = SESSIONS.create(ttl)
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=ttl,
        path="/",
        httponly=True,
        samesite="strict",
        secure=cookie_secure(request, settings),
    )
    log_event(logger, logging.INFO, "auth.login_succeeded", ttl_seconds=ttl)
    return response


@router.post("/logout", response_model=None)
async def logout(request: Request) -> Response:
    """End the operator session (form-encoded `csrf_token`)."""
    raw = request.cookies.get(SESSION_COOKIE)
    session = SESSIONS.get(raw)
    if session is not None:
        form = await _read_form(request) or {}
        if same_origin(request) is not True or not csrf_valid(form.get("csrf_token"), session):
            log_event(logger, logging.WARNING, "auth.logout_rejected")
            return JSONResponse(status_code=403, content={"detail": "missing or invalid CSRF token"})
        SESSIONS.revoke(raw)
        log_event(logger, logging.INFO, "auth.logout")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@router.get("/api/session")
async def session_info(request: Request) -> dict[str, Any]:
    """Describe the caller's access mode and session expiry (never returns credentials)."""
    ctx = operator_context(request)
    return {
        "mode": effective_access_mode(get_settings()),
        "method": ctx.method if ctx else None,
        "expires_at": expires_at_iso(ctx),
    }
