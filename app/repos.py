"""Helpers for parsing GitHub repository URLs / slugs and Droid launch config."""

from urllib.parse import urlsplit

from app.database import db_session
from app.models import Repository

_REPOSITORY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
# Anything else (whitespace, %, ?, #, backslash, ...) is rejected before URL parsing, so
# urlsplit's own normalization (it silently drops tabs/newlines) cannot change the host.
_INPUT_CHARS = _REPOSITORY_CHARS | frozenset("/:@")
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_DOT_SEGMENTS = frozenset({".", ".."})
_SSH_USER = "git"


def parse_repository_ref(value: str) -> str | None:
    """Return owner/repo from a GitHub reference, or None if invalid.

    Accepted forms: ``owner/repo``; ``http(s)://`` or scheme-less
    ``(www.)github.com/owner/repo``; ``git@github.com:owner/repo`` and
    ``ssh://git@github.com/owner/repo``. Hosts match exactly (case-insensitive);
    credentials, ports, query/fragment and ``.``/``..`` components are rejected.
    URL and SSH forms drop one trailing ``.git`` when a name remains; slugs are kept as given.
    """
    raw = (value or "").strip()
    if not raw or not _INPUT_CHARS.issuperset(raw):
        return None
    if "://" in raw:
        return _parse_url(raw)
    if raw.startswith(f"{_SSH_USER}@"):
        return _parse_scp_ssh(raw)
    first, separator, rest = raw.partition("/")
    if separator and first.lower() in _GITHUB_HOSTS:
        return _repository_path(rest)
    return _parse_slug(raw.rstrip("/"))


def _parse_url(raw: str) -> str | None:
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme in ("https", "http"):
        host = parts.netloc
    elif scheme == "ssh":
        user, at, host = parts.netloc.partition("@")
        if not at or user != _SSH_USER:
            return None
    else:
        return None
    if host.lower() not in _GITHUB_HOSTS or not parts.path.startswith("/"):
        return None
    return _repository_path(parts.path[1:])


def _parse_scp_ssh(raw: str) -> str | None:
    host, separator, path = raw[len(_SSH_USER) + 1 :].partition(":")
    if not separator or host.lower() not in _GITHUB_HOSTS:
        return None
    return _repository_path(path)


def _repository_path(path: str) -> str | None:
    owner, separator, name = path.rstrip("/").partition("/")
    if separator and len(name) > 4 and name.endswith(".git"):
        name = name[:-4]
    if separator and _valid_repository_part(owner) and _valid_repository_part(name):
        return f"{owner}/{name}"
    return None


def _parse_slug(raw: str) -> str | None:
    owner, separator, name = raw.partition("/")
    if separator and _valid_repository_part(owner) and _valid_repository_part(name):
        return raw
    return None


def _valid_repository_part(value: str) -> bool:
    return bool(value) and value not in _DOT_SEGMENTS and _REPOSITORY_CHARS.issuperset(value)


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
