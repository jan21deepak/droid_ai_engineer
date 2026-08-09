"""Helpers for parsing GitHub repository URLs / slugs."""

import re

_GITHUB_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def parse_repository_ref(value: str) -> str | None:
    """Return owner/repo from a GitHub URL or slug, or None if invalid."""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return None
    match = _GITHUB_REPO_RE.match(raw)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw):
        return raw
    return None
