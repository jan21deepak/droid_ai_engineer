"""Repository, ref and outward-action policy shared by every entry path.

Configured settings, not agent instructions or UI labels, decide what the app
may touch:

- ``FORGE_ALLOWED_REPOSITORIES``: repositories that may be registered, read,
  worked on, reviewed and polled.
- ``FORGE_ALLOWED_BRANCHES``: branches the app may push or open a PR from.
- ``FORGE_ALLOW_GITHUB_WRITES``: master switch for every GitHub write.
- ``FORGE_ALLOW_PRIVILEGED_SETUP``: stored setup commands only run with this.

Merging, enabling auto-merge and installing webhooks are never permitted
outward actions. Every check reads current settings, so changed configuration
also applies to work stored before the change.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func

from app.config import ACCESS_MODE_LOCAL_DEV, Settings, effective_access_mode, get_settings, split_csv
from app.database import db_session
from app.models import Repository
from app.repos import parse_repository_ref


class OutwardAction:
    ISSUE_COMMENT = "issue_comment"
    ISSUE_CREATE = "issue_create"
    ISSUE_LABEL = "issue_label"
    PULL_REQUEST_CREATE = "pull_request_create"
    PULL_REQUEST_REVIEW = "pull_request_review"
    BRANCH_PUSH = "branch_push"
    PULL_REQUEST_MERGE = "pull_request_merge"
    AUTO_MERGE_ENABLE = "auto_merge_enable"
    WEBHOOK_INSTALL = "webhook_install"

    PERMITTED = frozenset(
        {ISSUE_COMMENT, ISSUE_CREATE, ISSUE_LABEL, PULL_REQUEST_CREATE, PULL_REQUEST_REVIEW, BRANCH_PUSH}
    )
    REF_SCOPED = frozenset({PULL_REQUEST_CREATE, BRANCH_PUSH})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


ALLOW = PolicyDecision(True)


class OutwardActionDeniedError(RuntimeError):
    """Raised before an outward call that current policy does not permit."""

    def __init__(self, action: str, repository: str, decision: PolicyDecision):
        super().__init__(f"{action} on {repository} denied by policy: {decision.reason}")
        self.action = action
        self.repository = repository
        self.decision = decision


@dataclass(frozen=True)
class ApprovedRepository:
    full_name: str
    url: str


def allowed_repositories(settings: Settings) -> frozenset[str]:
    return frozenset(item.lower() for item in split_csv(settings.forge_allowed_repositories))


def allowed_branches(settings: Settings) -> frozenset[str]:
    return frozenset(split_csv(settings.forge_allowed_branches))


def _normalize_repository(repository: str) -> str:
    return (parse_repository_ref(repository or "") or (repository or "").strip()).lower()


def _normalize_ref(ref: str) -> str:
    value = (ref or "").strip()
    if value.startswith("refs/heads/"):
        value = value[len("refs/heads/") :]
    # owner:branch PR heads name the branch after the colon.
    return value.split(":", 1)[-1]


def repository_decision(repository: str, settings: Settings | None = None) -> PolicyDecision:
    settings = settings or get_settings()
    name = _normalize_repository(repository)
    if not name:
        return PolicyDecision(False, "repository is missing")
    if repository_policy_allows(name, settings):
        return ALLOW
    return PolicyDecision(False, "repository is not in FORGE_ALLOWED_REPOSITORIES")


def ref_decision(ref: str, settings: Settings | None = None) -> PolicyDecision:
    settings = settings or get_settings()
    branch = _normalize_ref(ref)
    if not branch:
        return PolicyDecision(False, "branch is missing")
    allowed = allowed_branches(settings)
    if not allowed:
        if effective_access_mode(settings) == ACCESS_MODE_LOCAL_DEV:
            return ALLOW
        return PolicyDecision(False, "FORGE_ALLOWED_BRANCHES is empty")
    if branch in allowed:
        return ALLOW
    return PolicyDecision(False, f"branch {branch!r} is not in FORGE_ALLOWED_BRANCHES")


def outward_decision(
    action: str,
    repository: str,
    *,
    ref: str | None = None,
    settings: Settings | None = None,
) -> PolicyDecision:
    """Whether the app may perform ``action`` against GitHub right now."""
    settings = settings or get_settings()
    if action not in OutwardAction.PERMITTED:
        return PolicyDecision(False, f"{action} is never permitted")
    if not settings.forge_allow_github_writes:
        return PolicyDecision(False, "GitHub writes are disabled (FORGE_ALLOW_GITHUB_WRITES=false)")
    repo = repository_decision(repository, settings)
    if not repo.allowed:
        return repo
    if action in OutwardAction.REF_SCOPED:
        return ref_decision(ref or "", settings)
    return ALLOW


def require_outward(action: str, repository: str, *, ref: str | None = None) -> None:
    decision = outward_decision(action, repository, ref=ref)
    if not decision.allowed:
        raise OutwardActionDeniedError(action, repository, decision)


def privileged_setup_allowed(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).forge_allow_privileged_setup)


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
