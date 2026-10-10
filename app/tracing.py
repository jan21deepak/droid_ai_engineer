"""Request-ID propagation for tracing requests across logs.

Every HTTP request gets an ``X-Request-ID`` (echoed from the caller's header or
generated), exposed to log records via a context variable and returned in the
response so callers can correlate their requests with server-side log lines.
"""

from __future__ import annotations

import contextvars
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"


def current_request_id() -> str:
    """The request id bound to the current execution context ('-' outside requests)."""
    return request_id_ctx.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assign/propagate X-Request-ID for every request and stamp it into logs."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
