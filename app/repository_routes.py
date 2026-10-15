"""Repository registration, configuration and GitHub sync routes.

Registration and every GitHub-touching route check repository policy before
any external call; outward writes (labels, issues, webhooks) are additionally
checked against outward-action policy. Stored setup commands are shown with
their execution status so the operator can see they are disabled by default.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.database import db_session
from app.github import GitHubClient
from app.logging_conf import log_event
from app.models import Repository, SyncedIssue, SyncedPullRequest
from app.policy import OutwardAction, outward_decision, privileged_setup_allowed, repository_decision
from app.redaction import redact, safe_error
from app.repos import parse_repository_ref

logger = logging.getLogger("app.api")
router = APIRouter()

GITHUB_REJECTION_FALLBACK = "GitHub rejected the issue"


def github_rejection_detail(response: httpx.Response) -> str:
    """Redacted, bounded text for a GitHub error response, whatever shape its body has."""
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    message = data.get("message") if isinstance(data, dict) else data
    if message is None or message == "":
        text = response.text.strip()
    elif isinstance(message, str):
        text = message
    else:
        # Serialize after masking sensitive keys so values and keys both pass the text scrubber.
        text = json.dumps(redact(message), default=str)
    return safe_error(text) if text else GITHUB_REJECTION_FALLBACK


def repository_payload(repo: Repository) -> dict[str, Any]:
    """API view of a repository including current policy and setup-command status."""
    payload = repo.to_dict()
    decision = repository_decision(repo.full_name)
    if not (repo.setup_command or "").strip():
        setup = "not_configured"
    elif privileged_setup_allowed():
        setup = "enabled"
    else:
        setup = "disabled_by_policy"
    payload["setup_command_execution"] = setup
    payload["policy"] = {"allowed": decision.allowed, "reason": decision.reason}
    return payload


def _repository_denied(full_name: str) -> JSONResponse | None:
    decision = repository_decision(full_name)
    if decision.allowed:
        return None
    log_event(logger, logging.WARNING, "policy.repository_denied", repo=full_name[:200], reason=decision.reason)
    return JSONResponse(
        status_code=403,
        content={"detail": "repository is not approved by policy", "reason": decision.reason},
    )


async def ensure_repo_automation(github: GitHubClient, full_name: str) -> dict[str, Any]:
    """Ensure trigger label + issues webhook exist, as far as outward-action policy permits."""
    settings = get_settings()
    out: dict[str, Any] = {"label": None, "webhook": None}
    label_policy = outward_decision(OutwardAction.ISSUE_LABEL, full_name)
    if not label_policy.allowed:
        out["label"] = {"skipped": label_policy.reason}
    else:
        try:
            out["label"] = await github.ensure_label(full_name, settings.trigger_label)
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "github.label_ensure_failed",
                repo=full_name,
                error=str(exc),
            )
            out["label"] = False

    public = (settings.public_base_url or "").strip().rstrip("/")
    hook_policy = outward_decision(OutwardAction.WEBHOOK_INSTALL, full_name)
    if public and not hook_policy.allowed:
        out["webhook"] = {"skipped": hook_policy.reason}
    elif public:
        try:
            out["webhook"] = await github.ensure_issues_webhook(
                full_name,
                webhook_url=f"{public}/webhook",
                secret=settings.github_webhook_secret,
            )
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "github.webhook_ensure_failed",
                repo=full_name,
                error=str(exc),
            )
            out["webhook"] = {"error": safe_error(exc)}
    return out


class AddRepositoryRequest(BaseModel):
    url: str = Field(..., description="GitHub repository URL or owner/repo slug")
    droid_model: str = Field(
        default="",
        description="Optional per-repo model id override (empty = DROID_MODEL)",
    )
    setup_command: str = Field(
        default="",
        description=(
            "Optional shell command for the workspace clone; stored only. It runs before the session "
            "only when FORGE_ALLOW_PRIVILEGED_SETUP=true"
        ),
    )
    starting_ref: str = Field(
        default="",
        description="Optional git ref used when cloning the workspace",
    )


class UpdateRepositoryRequest(BaseModel):
    droid_model: str | None = Field(
        default=None,
        description="Optional per-repo model id override (empty = DROID_MODEL)",
    )
    setup_command: str | None = Field(
        default=None,
        description=(
            "Optional shell command for the workspace clone; stored only. It runs before the session "
            "only when FORGE_ALLOW_PRIVILEGED_SETUP=true"
        ),
    )
    starting_ref: str | None = Field(
        default=None,
        description="Optional git ref used when cloning the workspace",
    )


@router.get("/api/repositories")
async def list_repositories() -> dict[str, Any]:
    with db_session() as session:
        repos = session.query(Repository).order_by(Repository.created_at.desc()).all()
        return {"repositories": [repository_payload(r) for r in repos]}


@router.post("/api/repositories", response_model=None)
async def add_repository(payload: AddRepositoryRequest) -> JSONResponse | dict[str, Any]:
    full_name = parse_repository_ref(payload.url)
    if not full_name:
        return JSONResponse(status_code=422, content={"detail": "invalid GitHub repository URL or slug"})
    denied = _repository_denied(full_name)
    if denied is not None:
        return denied

    github = GitHubClient()
    try:
        meta = await github.get_repository(full_name)
    except httpx.HTTPStatusError as exc:
        detail = "repository not found" if exc.response.status_code == 404 else "failed to fetch repository"
        return JSONResponse(status_code=exc.response.status_code, content={"detail": detail})
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"detail": f"GitHub request failed: {safe_error(exc)}"})
    # GitHub may answer with a renamed canonical name; that name must be approved too.
    denied = _repository_denied(meta.get("full_name") or full_name)
    if denied is not None:
        return denied

    with db_session() as session:
        existing = session.query(Repository).filter(Repository.full_name == full_name).first()
        if existing:
            return {"detail": "already registered", "repository": repository_payload(existing)}
        repo = Repository(
            full_name=meta.get("full_name") or full_name,
            url=meta.get("html_url") or f"https://github.com/{full_name}",
            description=(meta.get("description") or "")[:2000],
            droid_model=(payload.droid_model or "").strip(),
            setup_command=(payload.setup_command or "").strip(),
            starting_ref=(payload.starting_ref or "").strip() or (meta.get("default_branch") or "main") or "main",
        )
        session.add(repo)
        session.flush()
        repo_dict = repository_payload(repo)
        log_event(
            logger,
            logging.INFO,
            "repository.added",
            repo=repo.full_name,
            model=repo.droid_model or None,
            setup_command=bool(repo.setup_command),
        )

    automation = await ensure_repo_automation(github, full_name)
    return JSONResponse(
        status_code=201,
        content={"repository": repo_dict, "automation": automation},
    )


@router.patch("/api/repositories/{repo_id}", response_model=None)
async def update_repository(repo_id: int, payload: UpdateRepositoryRequest) -> JSONResponse | dict[str, Any]:
    """Update Droid model / setup command / starting ref for a registered repository."""
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        if payload.droid_model is not None:
            repo.droid_model = payload.droid_model.strip()
        if payload.setup_command is not None:
            repo.setup_command = payload.setup_command.strip()
        if payload.starting_ref is not None:
            repo.starting_ref = payload.starting_ref.strip()
        session.flush()
        log_event(
            logger,
            logging.INFO,
            "repository.updated",
            repo=repo.full_name,
            model=repo.droid_model or None,
            starting_ref=repo.starting_ref or None,
            setup_command=bool(repo.setup_command),
        )
        return {"repository": repository_payload(repo)}


@router.delete("/api/repositories/{repo_id}", response_model=None)
async def delete_repository(repo_id: int) -> JSONResponse | dict[str, Any]:
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        full_name = repo.full_name
        session.query(SyncedIssue).filter(SyncedIssue.repository == full_name).delete()
        session.query(SyncedPullRequest).filter(SyncedPullRequest.repository == full_name).delete()
        session.delete(repo)
    log_event(logger, logging.INFO, "repository.removed", repo=full_name)
    return {"detail": "removed"}


def _persist_synced_issues(full_name: str, repo_url: str, issues: list[dict[str, Any]]) -> int:
    """Replace the synced issues for a repository with the supplied open issues."""
    deduped: dict[int, dict[str, Any]] = {}
    for item in issues:
        number = int(item.get("number") or 0)
        if not number or (item.get("state") or "open") != "open":
            continue
        deduped[number] = item

    now = datetime.now(UTC)
    with db_session() as session:
        session.query(SyncedIssue).filter(SyncedIssue.repository == full_name).delete()
        for number, item in sorted(deduped.items(), reverse=True):
            labels = [lbl.get("name", "") for lbl in item.get("labels") or []]
            session.add(
                SyncedIssue(
                    repository=full_name,
                    repository_url=repo_url,
                    issue_number=number,
                    title=item.get("title") or "",
                    body=item.get("body") or "",
                    state="open",
                    labels=json.dumps(labels),
                    html_url=item.get("html_url") or "",
                    synced_at=now,
                )
            )
    return len(deduped)


def _persist_synced_pulls(full_name: str, repo_url: str, pulls: list[dict[str, Any]]) -> int:
    """Replace synced open PRs (drafts included, e.g. Forge's own draft PRs) for a repository."""
    deduped: dict[int, dict[str, Any]] = {}
    for item in pulls:
        number = int(item.get("number") or 0)
        if not number:
            continue
        if (item.get("state") or "open") != "open":
            continue
        deduped[number] = item

    now = datetime.now(UTC)
    with db_session() as session:
        session.query(SyncedPullRequest).filter(SyncedPullRequest.repository == full_name).delete()
        for number, item in sorted(deduped.items(), reverse=True):
            user = item.get("user") or {}
            head = item.get("head") or {}
            session.add(
                SyncedPullRequest(
                    repository=full_name,
                    repository_url=repo_url,
                    pr_number=number,
                    title=item.get("title") or "",
                    body=item.get("body") or "",
                    html_url=item.get("html_url") or "",
                    head_sha=(head.get("sha") or "")[:64],
                    author=user.get("login") or "",
                    draft=bool(item.get("draft")),
                    synced_at=now,
                )
            )
    return len(deduped)


async def sync_repository_pulls(repo_id: int) -> dict[str, Any]:
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return {"error": "repository not found", "status": 404}
        full_name = repo.full_name
        repo_url = repo.url
    decision = repository_decision(full_name)
    if not decision.allowed:
        return {"error": f"repository not approved by policy: {decision.reason}", "status": 403}

    github = GitHubClient()
    try:
        pulls = await github.list_open_pull_requests(full_name)
    except httpx.HTTPError as exc:
        return {"error": f"failed to list pull requests: {safe_error(exc)}", "status": 502}

    synced = _persist_synced_pulls(full_name, repo_url, pulls)
    log_event(logger, logging.INFO, "pulls.synced", repo=full_name, count=synced)
    return {"detail": "synced", "repository": full_name, "count": synced}


@router.post("/api/repositories/{repo_id}/sync-issues", response_model=None)
async def sync_repository_issues(repo_id: int) -> JSONResponse | dict[str, Any]:
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return JSONResponse(status_code=404, content={"detail": "repository not found"})
        full_name = repo.full_name
        repo_url = repo.url
    denied = _repository_denied(full_name)
    if denied is not None:
        return denied

    github = GitHubClient()
    try:
        issues = await github.list_open_issues(full_name)
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"detail": f"failed to list issues: {safe_error(exc)}"})

    synced = _persist_synced_issues(full_name, repo_url, issues)
    pulls_result = await sync_repository_pulls(repo_id)
    log_event(logger, logging.INFO, "issues.synced", repo=full_name, count=synced)
    return {
        "detail": "synced",
        "repository": full_name,
        "count": synced,
        "pulls_count": pulls_result.get("count", 0),
    }


@router.post("/api/repositories/{repo_id}/issues", response_model=None)
async def create_repository_issues(repo_id: int) -> JSONResponse | dict[str, Any]:
    """Copy five new open issues from a fork's parent into the repository."""
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return JSONResponse(status_code=404, content={"detail": "repository not found"})
        full_name = repo.full_name
        repo_url = repo.url
    write = outward_decision(OutwardAction.ISSUE_CREATE, full_name)
    if not write.allowed:
        return JSONResponse(status_code=403, content={"detail": f"issue creation denied by policy: {write.reason}"})

    github = GitHubClient()
    try:
        metadata = await github.get_repository(full_name)
        parent = (metadata.get("parent") or {}).get("full_name")
        if not parent:
            return JSONResponse(
                status_code=422,
                content={"detail": f"{full_name} is not a fork and has no parent repository"},
            )
        parent_issues, existing_issues = await asyncio.gather(
            github.list_open_issues(parent),
            github.list_issues(full_name, state="all"),
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"failed to discover parent issues: {safe_error(exc)}"},
        )

    existing_bodies = "\n".join(item.get("body") or "" for item in existing_issues)
    candidates = [item for item in parent_issues if f"Source issue: {item.get('html_url')}" not in existing_bodies][:5]
    if not candidates:
        return {
            "detail": "no_new_parent_issues",
            "repository": full_name,
            "parent": parent,
            "created": [],
            "synced_count": len(existing_issues),
        }

    created = []
    created_payloads: list[dict[str, Any]] = []
    try:
        for source in candidates:
            source_url = source.get("html_url") or (f"https://github.com/{parent}/issues/{source.get('number')}")
            source_body = (source.get("body") or "").strip()
            body = (
                f"{source_body}\n\n"
                "---\n"
                "Imported from the parent repository for autonomous "
                "engineering evaluation.\n\n"
                f"Source issue: {source_url}"
            ).strip()
            issue = await github.create_issue(
                full_name,
                source.get("title") or f"Parent issue #{source.get('number')}",
                body,
            )
            created_payloads.append(issue)
            created.append(
                {
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "html_url": issue.get("html_url"),
                    "source_url": source_url,
                }
            )
    except httpx.HTTPStatusError as exc:
        return JSONResponse(
            status_code=exc.response.status_code,
            content={"detail": github_rejection_detail(exc.response), "created": redact(created)},
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"failed to create issue: {safe_error(exc)}", "created": redact(created)},
        )

    # Keep the selection tab current without requiring a second button click.
    # GitHub's issue list lags behind creation, so merge in what we just created.
    try:
        listed = await github.list_open_issues(full_name)
    except httpx.HTTPError:
        listed = []
    synced_count = _persist_synced_issues(full_name, repo_url, listed + created_payloads)

    return {
        "detail": "created",
        "repository": full_name,
        "parent": parent,
        "created": created,
        "synced_count": synced_count,
    }
