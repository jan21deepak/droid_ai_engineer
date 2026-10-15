"""Helpers for parsing GitHub repository URLs / slugs and Droid launch config."""

from app.database import db_session
from app.models import Repository

_REPOSITORY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")


def parse_repository_ref(value: str) -> str | None:
    """Return owner/repo from a GitHub URL or slug, or None if invalid."""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return None
    slug = raw
    for prefix in ("https://", "http://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    if raw.startswith("www."):
        raw = raw[4:]
    if raw.startswith("github.com/"):
        owner, separator, name = raw[len("github.com/") :].partition("/")
        if separator and len(name) > 4 and name.endswith(".git"):
            name = name[:-4]
        if separator and _valid_repository_part(owner) and _valid_repository_part(name):
            return f"{owner}/{name}"
        return None
    owner, separator, name = slug.partition("/")
    if separator and _valid_repository_part(owner) and _valid_repository_part(name):
        return slug
    return None


def _valid_repository_part(value: str) -> bool:
    return bool(value) and _REPOSITORY_CHARS.issuperset(value)


def resolve_agent_launch_config(
    repository: str | None = None,
    repository_url: str | None = None,
) -> dict[str, str | None]:
    """Look up the Droid launch config (model, setup command, starting ref).

    Returns ``{"model": str|None, "setup_command": str|None,
    "starting_ref": str|None}``. Missing registrations yield empty config so
    callers fall back to the global model and a bare clone.
    """
    full_name = parse_repository_ref(repository or "") or parse_repository_ref(repository_url or "")
    if not full_name and repository and "/" in repository:
        full_name = repository.strip()
    if not full_name:
        return {"model": None, "setup_command": None, "starting_ref": None}

    with db_session() as session:
        row = session.query(Repository).filter(Repository.full_name == full_name).first()
        if not row:
            # Case-insensitive fallback (GitHub full_name is usually canonical).
            row = session.query(Repository).filter(Repository.full_name.ilike(full_name)).first()
        if not row:
            return {"model": None, "setup_command": None, "starting_ref": None}
        return {
            "model": (row.droid_model or "").strip() or None,
            "setup_command": (row.setup_command or "").strip() or None,
            "starting_ref": (row.starting_ref or "").strip() or "main",
        }
