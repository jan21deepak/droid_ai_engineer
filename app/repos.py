"""Helpers for parsing GitHub repository URLs / slugs and Droid launch config."""

import re

from app.database import db_session
from app.models import Repository

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


def resolve_agent_launch_config(
    repository: str | None = None,
    repository_url: str | None = None,
) -> dict[str, str | None]:
    """Look up the Droid launch config (model, setup command, starting ref).

    Returns ``{"model": str|None, "setup_command": str|None,
    "starting_ref": str|None}``. Missing registrations yield empty config so
    callers fall back to the global model and a bare clone.
    """
    full_name = parse_repository_ref(repository or "") or parse_repository_ref(
        repository_url or ""
    )
    if not full_name and repository and "/" in repository:
        full_name = repository.strip()
    if not full_name:
        return {"model": None, "setup_command": None, "starting_ref": None}

    with db_session() as session:
        row = (
            session.query(Repository)
            .filter(Repository.full_name == full_name)
            .first()
        )
        if not row:
            # Case-insensitive fallback (GitHub full_name is usually canonical).
            row = (
                session.query(Repository)
                .filter(Repository.full_name.ilike(full_name))
                .first()
            )
        if not row:
            return {"model": None, "setup_command": None, "starting_ref": None}
        return {
            "model": (row.droid_model or "").strip() or None,
            "setup_command": (row.setup_command or "").strip() or None,
            "starting_ref": (row.starting_ref or "").strip() or "main",
        }
