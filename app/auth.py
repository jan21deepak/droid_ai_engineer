"""Single-operator access control: sessions, bearer auth, CSRF, Origin and Host checks.

Every route is protected by default; only the paths in ``PUBLIC_PATHS`` /
``PUBLIC_PREFIXES`` bypass operator authentication, and those that accept
writes (login, logout, webhook) perform their own verification. Forwarded
headers (``X-Forwarded-*``, ``Forwarded``) are never consulted.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from app.config import ACCESS_MODE_LOCAL_DEV, Settings, effective_access_mode, get_settings, split_csv
from app.logging_conf import log_event

logger = logging.getLogger("app.auth")

SESSION_COOKIE = "forge_session"
CSRF_HEADER = "x-csrf-token"
MIN_OPERATOR_TOKEN_LENGTH = 32
MAX_SESSIONS = 32
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_PATHS = frozenset({"/health", "/login", "/logout", "/webhook", "/favicon.ico"})
PUBLIC_PREFIXES = ("/static/",)

AUTH_METHOD_SESSION = "session"
AUTH_METHOD_BEARER = "bearer"
AUTH_METHOD_LOCAL_DEV = "local_dev"


@dataclass(frozen=True)
class OperatorSession:
    csrf_token: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class OperatorContext:
    """Who is making the current request; stored on ``request.state.operator``."""

    method: str
    csrf_token: str = ""
    expires_at: float | None = None


class SessionStore:
    """In-memory server-side sessions keyed by a hash of the cookie value.

    Process-local by design: the service is a single Uvicorn process, and a
    restart (including after rotating the operator token) logs everyone out.
    """

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: dict[str, OperatorSession] = {}
        self._max = max_sessions
        self.now: Callable[[], float] = time.time

    @staticmethod
    def _key(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def create(self, ttl_seconds: int) -> tuple[str, OperatorSession]:
        self._purge_expired()
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].created_at)
            del self._sessions[oldest]
        raw = secrets.token_urlsafe(32)
        now = self.now()
        record = OperatorSession(
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + max(ttl_seconds, 1),
        )
        self._sessions[self._key(raw)] = record
        return raw, record

    def get(self, raw: str | None) -> OperatorSession | None:
        if not raw:
            return None
        key = self._key(raw)
        record = self._sessions.get(key)
        if record is None:
            return None
        if record.expires_at <= self.now():
            del self._sessions[key]
            return None
        return record

    def revoke(self, raw: str | None) -> None:
        if raw:
            self._sessions.pop(self._key(raw), None)

    def clear(self) -> None:
        self._sessions.clear()

    def _purge_expired(self) -> None:
        now = self.now()
        for key in [k for k, v in self._sessions.items() if v.expires_at <= now]:
            del self._sessions[key]


SESSIONS = SessionStore()


def reset_sessions_for_tests() -> None:
    SESSIONS.clear()
    SESSIONS.now = time.time


def tokens_match(provided: str, expected: str) -> bool:
    """Constant-time credential comparison (digests hide length differences)."""
    a = hashlib.sha256(provided.encode("utf-8")).digest()
    b = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


def operator_credential_problem(settings: Settings) -> str | None:
    """Why protected mode cannot authenticate anyone, or None when it can."""
    token = settings.forge_operator_token or ""
    if not token.strip():
        return "FORGE_OPERATOR_TOKEN is not configured"
    if len(token) < MIN_OPERATOR_TOKEN_LENGTH:
        return f"FORGE_OPERATOR_TOKEN must be at least {MIN_OPERATOR_TOKEN_LENGTH} characters"
    return None


def verify_operator_token(provided: str, settings: Settings) -> bool:
    if operator_credential_problem(settings) is not None or not provided:
        return False
    return tokens_match(provided, settings.forge_operator_token)


def _split_host(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def trusted_hosts(settings: Settings) -> set[str]:
    hosts = {_split_host(h) for h in split_csv(settings.forge_trusted_hosts)}
    public = (settings.public_base_url or "").strip()
    if public:
        public_host = urlsplit(public).hostname
        if public_host:
            hosts.add(public_host.lower())
    return hosts


def host_allowed(request: Request, settings: Settings) -> bool:
    host_header = request.headers.get("host", "")
    return bool(host_header) and _split_host(host_header) in trusted_hosts(settings)


def same_origin(request: Request) -> bool | None:
    """True/False when Origin (or Referer) proves/disproves same-origin; None when absent."""
    source = request.headers.get("origin")
    if source is None:
        source = request.headers.get("referer")
        if not source:
            return None
    if source.strip().lower() == "null":
        return False
    parsed = urlsplit(source.strip())
    host_header = request.headers.get("host", "").strip().lower()
    return parsed.scheme == request.url.scheme and bool(host_header) and parsed.netloc.lower() == host_header


def _is_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def cookie_secure(request: Request, settings: Settings) -> bool:
    configured = (settings.forge_cookie_secure or "auto").strip().lower()
    if configured == "true":
        return True
    if configured == "false":
        return False
    if request.url.scheme == "https":
        return True
    # Plain-HTTP loopback is the documented local demo exception.
    return not _is_loopback(_split_host(request.headers.get("host", "")))


def csrf_valid(provided: str | None, session: OperatorSession) -> bool:
    return bool(provided) and tokens_match(provided or "", session.csrf_token)


def safe_next(target: str | None) -> str:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/dashboard"
    return target


def operator_context(request: Request) -> OperatorContext | None:
    ctx = getattr(request.state, "operator", None)
    return ctx if isinstance(ctx, OperatorContext) else None


def expires_at_iso(ctx: OperatorContext | None) -> str | None:
    if ctx is None or ctx.expires_at is None:
        return None
    return datetime.fromtimestamp(ctx.expires_at, UTC).isoformat()


def _wants_html(request: Request) -> bool:
    return request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", "")


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _reject(request: Request, status: int, detail: str, reason: str) -> Response:
    log_event(
        logger,
        logging.WARNING,
        "auth.request_rejected",
        reason=reason,
        method=request.method,
        path=request.url.path,
    )
    if status == 503 and _wants_html(request):
        return PlainTextResponse(f"Operator access unavailable: {detail}", status_code=503)
    return JSONResponse(status_code=status, content={"detail": detail})


def _unauthenticated(request: Request, had_cookie: bool) -> Response:
    if _wants_html(request):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        response: Response = RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    else:
        response = JSONResponse(
            status_code=401,
            content={"detail": "authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if had_cookie:
        response.delete_cookie(SESSION_COOKIE, path="/")
    log_event(
        logger,
        logging.INFO,
        "auth.unauthenticated",
        method=request.method,
        path=request.url.path,
        stale_cookie=had_cookie,
    )
    return response


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class AccessControlMiddleware(BaseHTTPMiddleware):
    """Default-deny operator authentication for every non-public route."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        if not host_allowed(request, settings):
            return _reject(request, 400, "untrusted host", "untrusted_host")
        if _is_public(request.url.path):
            return await call_next(request)

        unsafe = request.method not in SAFE_METHODS
        if effective_access_mode(settings) == ACCESS_MODE_LOCAL_DEV:
            # Browsers always send Origin on cross-site writes; local tools usually do not.
            if unsafe and same_origin(request) is False:
                return _reject(request, 403, "cross-origin request rejected", "cross_origin")
            request.state.operator = OperatorContext(method=AUTH_METHOD_LOCAL_DEV)
            return await call_next(request)

        problem = operator_credential_problem(settings)
        if problem is not None:
            return _reject(request, 503, "operator access is not configured", "operator_unconfigured")

        bearer = _bearer_token(request)
        if bearer is not None:
            if not verify_operator_token(bearer, settings):
                return _reject(request, 401, "authentication required", "invalid_bearer")
            request.state.operator = OperatorContext(method=AUTH_METHOD_BEARER)
            return await call_next(request)

        raw_cookie = request.cookies.get(SESSION_COOKIE)
        session = SESSIONS.get(raw_cookie)
        if session is None:
            return _unauthenticated(request, had_cookie=raw_cookie is not None)
        if unsafe:
            if same_origin(request) is not True:
                return _reject(request, 403, "cross-origin request rejected", "origin_mismatch")
            if not csrf_valid(request.headers.get(CSRF_HEADER), session):
                return _reject(request, 403, "missing or invalid CSRF token", "csrf_invalid")
        request.state.operator = OperatorContext(
            method=AUTH_METHOD_SESSION,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )
        return await call_next(request)
