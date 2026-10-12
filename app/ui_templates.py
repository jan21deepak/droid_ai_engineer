"""Shared Jinja templates and static asset versioning for the operator UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__
from app.auth import operator_context

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def asset_version() -> str:
    """Cache-busting token for static assets.

    Browsers otherwise hold onto scripts/styles across deploys and silently run
    stale UI code against a newer API.
    """
    try:
        newest = max(path.stat().st_mtime for path in STATIC_DIR.glob("*.*"))
    except ValueError:
        return __version__
    return f"{__version__}-{int(newest)}"


def page_context(request: Request, **extra: Any) -> dict[str, Any]:
    """Values every operator page needs: CSRF token, sign-in method and asset version."""
    operator = operator_context(request)
    return {
        "asset_version": asset_version(),
        "csrf_token": operator.csrf_token if operator else "",
        "auth_method": operator.method if operator else "",
        **extra,
    }
