"""Repository approval policy shared by intake and automation entry paths."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func

from app.config import ACCESS_MODE_LOCAL_DEV, Settings, effective_access_mode, split_csv
from app.database import db_session
from app.models import Repository


@dataclass(frozen=True)
class ApprovedRepository:
    full_name: str
    url: str


def allowed_repositories(settings: Settings) -> frozenset[str]:
    return frozenset(item.lower() for item in split_csv(settings.forge_allowed_repositories))


def repository_policy_allows(full_name: str, settings: Settings) -> bool:
    """Whether FORGE_ALLOWED_REPOSITORIES approves ``full_name``.

    An empty allowlist approves nothing in protected mode; explicit local_dev
    keeps the historical "any registered repository" behavior.
    """
    allowed = allowed_repositories(settings)
    if not allowed:
        return effective_access_mode(settings) == ACCESS_MODE_LOCAL_DEV
    return (full_name or "").strip().lower() in allowed


def registered_repository(full_name: str) -> ApprovedRepository | None:
    name = (full_name or "").strip().lower()
    if not name:
        return None
    with db_session() as session:
        row = session.query(Repository).filter(func.lower(Repository.full_name) == name).first()
        if row is None:
            return None
        return ApprovedRepository(full_name=row.full_name, url=row.url)


def approved_registered_repository(full_name: str, settings: Settings) -> ApprovedRepository | None:
    """The registered record for ``full_name`` when it is also policy-approved."""
    if not repository_policy_allows(full_name, settings):
        return None
    return registered_repository(full_name)
