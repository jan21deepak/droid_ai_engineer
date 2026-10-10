"""FastAPI application: webhook intake, dashboard, metrics and health endpoints."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import __version__
from app.analytics import fire_and_forget
from app.auth import AccessControlMiddleware, operator_context, operator_credential_problem
from app.auth_routes import router as auth_router
from app.config import ACCESS_MODE_PROTECTED, effective_access_mode, get_settings
from app.database import check_db_connectivity, db_session, init_db
from app.droid_client import (
    DroidSessionBusyError,
    get_droid_client,
)
from app.error_tracking import init_error_tracking
from app.github import GitHubClient
from app.intake import router as intake_router
from app.logging_conf import log_event, setup_logging
from app.metrics import compute_metrics, recent_activity
from app.models import Repository, ReviewTask, SyncedIssue, SyncedPullRequest, Task, TaskStatus
from app.repos import parse_repository_ref
from app.tasks import TaskCreateError, create_and_dispatch_task
from app.tracing import RequestIDMiddleware
from app.worker import recover_interrupted_tasks, start_pr_review, worker_loop

logger = logging.getLogger("app.api")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def asset_version() -> str:
    """Cache-busting token for static assets.

    Browsers otherwise hold onto dashboard.js/css across deploys and silently run
    stale UI code against a newer API.
    """
    try:
        newest = max(path.stat().st_mtime for path in STATIC_DIR.glob("dashboard.*"))
    except ValueError:
        return __version__
    return f"{__version__}-{int(newest)}"


async def _ensure_repo_automation(github: GitHubClient, full_name: str) -> dict[str, Any]:
    """Ensure trigger label + issues webhook exist for a registered repository."""
    settings = get_settings()
    out: dict[str, Any] = {"label": None, "webhook": None}
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
    if public:
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
            out["webhook"] = {"error": str(exc)}
    return out


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    init_error_tracking(settings.sentry_dsn, settings.sentry_environment)
    init_db()
    mode = effective_access_mode(settings)
    credential_problem = operator_credential_problem(settings) if mode == ACCESS_MODE_PROTECTED else None
    log_event(
        logger,
        logging.ERROR if credential_problem else logging.INFO,
        "auth.mode",
        mode=mode,
        operator_access="unavailable" if credential_problem else "ready",
        problem=credential_problem,
    )
    droid = get_droid_client()
    await recover_interrupted_tasks(droid)
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_loop(stop_event))
    log_event(logger, logging.INFO, "app.started", version=__version__, agent_runtime="droid-sdk")
    yield
    stop_event.set()
    await worker_task
    await droid.shutdown()


app = FastAPI(title="droid-ai-engineer", version=__version__, lifespan=lifespan)
# Default-deny operator access; added first so request tracing wraps rejections too.
app.add_middleware(AccessControlMiddleware)
# Request tracing: every request gets an X-Request-ID echoed into logs.
app.add_middleware(RequestIDMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(auth_router)
app.include_router(intake_router)


class AddRepositoryRequest(BaseModel):
    url: str = Field(..., description="GitHub repository URL or owner/repo slug")
    droid_model: str = Field(
        default="",
        description="Optional per-repo model id override (empty = DROID_MODEL)",
    )
    setup_command: str = Field(
        default="",
        description="Optional shell command run in the workspace clone before the session",
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
        description="Optional shell command run in the workspace clone before the session",
    )
    starting_ref: str | None = Field(
        default=None,
        description="Optional git ref used when cloning the workspace",
    )


class AssignIssuesRequest(BaseModel):
    issue_ids: list[int] = Field(..., min_length=1)


class AssignPullsRequest(BaseModel):
    pull_ids: list[int] = Field(..., min_length=1)


class FollowUpRequest(BaseModel):
    instruction: str = Field(
        ...,
        min_length=1,
        description="Follow-up prompt for the existing Droid session",
    )


@app.get("/")
async def root() -> RedirectResponse:
    """Send browsers to the dashboard UI (JSON API discovery lives under /health)."""
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/health")
async def health() -> JSONResponse:
    """Minimal public liveness: database reachability only, no external probes or versions."""
    db_ok = check_db_connectivity()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={"status": "ok" if db_ok else "unhealthy", "checks": {"database": "ok" if db_ok else "error"}},
    )


@app.get("/api/health")
async def health_details() -> JSONResponse:
    """Operator-only component status, including GitHub and Droid connectivity."""
    settings = get_settings()
    db_ok = check_db_connectivity()
    github_ok = await GitHubClient().check_connectivity()
    droid_ok = await get_droid_client().check_connectivity()

    if not db_ok:
        overall = "unhealthy"
        status_code = 503
    elif not github_ok or not droid_ok:
        overall = "degraded"
        status_code = 200
    else:
        overall = "healthy"
        status_code = 200

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "application": "ok",
            "version": __version__,
            "checks": {
                "database": "ok" if db_ok else "error",
                "github": "ok" if github_ok else ("unconfigured" if not settings.github_token else "error"),
                "droid": "ok" if droid_ok else ("unconfigured" if not settings.factory_api_key else "error"),
            },
        },
    )


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    with db_session() as session:
        return compute_metrics(session)


@app.get("/api/repositories")
async def list_repositories() -> dict[str, Any]:
    with db_session() as session:
        repos = session.query(Repository).order_by(Repository.created_at.desc()).all()
        return {"repositories": [r.to_dict() for r in repos]}


@app.get("/api/droid/models")
async def list_droid_models() -> dict[str, Any]:
    """Model catalog for the dashboard dropdown (Factory Router = "auto")."""
    droid = get_droid_client()
    models = await droid.list_available_models() if droid.configured else []
    return {"models": models, "count": len(models)}


@app.post("/api/repositories", response_model=None)
async def add_repository(payload: AddRepositoryRequest) -> JSONResponse | dict[str, Any]:
    full_name = parse_repository_ref(payload.url)
    if not full_name:
        return JSONResponse(status_code=422, content={"detail": "invalid GitHub repository URL or slug"})

    github = GitHubClient()
    try:
        meta = await github.get_repository(full_name)
    except httpx.HTTPStatusError as exc:
        detail = "repository not found" if exc.response.status_code == 404 else "failed to fetch repository"
        return JSONResponse(status_code=exc.response.status_code, content={"detail": detail})
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"detail": f"GitHub request failed: {exc}"})

    with db_session() as session:
        existing = session.query(Repository).filter(Repository.full_name == full_name).first()
        if existing:
            return {"detail": "already registered", "repository": existing.to_dict()}
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
        repo_dict = repo.to_dict()
        log_event(
            logger,
            logging.INFO,
            "repository.added",
            repo=repo.full_name,
            model=repo.droid_model or None,
            setup_command=bool(repo.setup_command),
        )

    automation = await _ensure_repo_automation(github, full_name)
    return JSONResponse(
        status_code=201,
        content={"repository": repo_dict, "automation": automation},
    )


@app.patch("/api/repositories/{repo_id}", response_model=None)
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
        return {"repository": repo.to_dict()}


@app.delete("/api/repositories/{repo_id}", response_model=None)
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
    """Replace synced open, non-draft PRs for a repository."""
    deduped: dict[int, dict[str, Any]] = {}
    for item in pulls:
        number = int(item.get("number") or 0)
        if not number or item.get("draft"):
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


async def _sync_repository_pulls(repo_id: int) -> dict[str, Any]:
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return {"error": "repository not found", "status": 404}
        full_name = repo.full_name
        repo_url = repo.url

    github = GitHubClient()
    try:
        pulls = await github.list_open_pull_requests(full_name)
    except httpx.HTTPError as exc:
        return {"error": f"failed to list pull requests: {exc}", "status": 502}

    synced = _persist_synced_pulls(full_name, repo_url, pulls)
    log_event(logger, logging.INFO, "pulls.synced", repo=full_name, count=synced)
    return {"detail": "synced", "repository": full_name, "count": synced}


@app.post("/api/repositories/{repo_id}/sync-issues", response_model=None)
async def sync_repository_issues(repo_id: int) -> JSONResponse | dict[str, Any]:
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return JSONResponse(status_code=404, content={"detail": "repository not found"})
        full_name = repo.full_name
        repo_url = repo.url

    github = GitHubClient()
    try:
        issues = await github.list_open_issues(full_name)
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"detail": f"failed to list issues: {exc}"})

    synced = _persist_synced_issues(full_name, repo_url, issues)
    pulls_result = await _sync_repository_pulls(repo_id)
    log_event(logger, logging.INFO, "issues.synced", repo=full_name, count=synced)
    return {
        "detail": "synced",
        "repository": full_name,
        "count": synced,
        "pulls_count": pulls_result.get("count", 0),
    }


@app.post("/api/repositories/{repo_id}/issues", response_model=None)
async def create_repository_issues(repo_id: int) -> JSONResponse | dict[str, Any]:
    """Copy five new open issues from a fork's parent into the repository."""
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return JSONResponse(status_code=404, content={"detail": "repository not found"})
        full_name = repo.full_name
        repo_url = repo.url

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
            content={"detail": f"failed to discover parent issues: {exc}"},
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
        detail = exc.response.json().get("message", "GitHub rejected the issue")
        return JSONResponse(
            status_code=exc.response.status_code,
            content={"detail": detail, "created": created},
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"failed to create issue: {exc}", "created": created},
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


@app.get("/api/issues")
async def list_synced_issues() -> dict[str, Any]:
    with db_session() as session:
        issues = (
            session.query(SyncedIssue).order_by(SyncedIssue.repository.asc(), SyncedIssue.issue_number.desc()).all()
        )
        active_keys = {
            (t.repository, t.issue_number)
            for t in session.query(Task).filter(
                Task.status.in_((TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.COMPLETED))
            )
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for issue in issues:
            payload = issue.to_dict()
            payload["assigned"] = (issue.repository, issue.issue_number) in active_keys
            grouped.setdefault(issue.repository, []).append(payload)
        return {"repositories": [{"repository": repo, "issues": items} for repo, items in grouped.items()]}


@app.post("/api/issues/assign", response_model=None)
async def assign_issues_to_droid(payload: AssignIssuesRequest) -> JSONResponse | dict[str, Any]:
    """Add the trigger label on GitHub so the webhook (flow 1) launches the agent.

    Always applies the trigger label on the issue. When ``PUBLIC_BASE_URL`` is
    configured, forge waits briefly for the webhook to create the task;
    otherwise it dispatches the Droid session directly after labeling
    (same end state, with duplicate protection if the webhook also fires).
    """
    settings = get_settings()
    github = GitHubClient()
    public = (settings.public_base_url or "").strip()
    prefer_webhook = bool(public)

    with db_session() as session:
        issues = session.query(SyncedIssue).filter(SyncedIssue.id.in_(payload.issue_ids)).all()
        snapshots = [issue.to_dict() for issue in issues]

    if not snapshots:
        return JSONResponse(status_code=404, content={"detail": "no matching issues"})

    fire_and_forget(
        "issues_assigned",
        {"count": len(payload.issue_ids), "via_webhook": bool(settings.public_base_url)},
    )
    results = []
    for issue in snapshots:
        labels = []
        try:
            labels = json.loads(issue.get("labels") or "[]")
        except json.JSONDecodeError:
            labels = []

        await github.ensure_label(issue["repository"], settings.trigger_label)
        labeled = True
        if settings.trigger_label not in labels:
            labeled = await github.add_issue_label(issue["repository"], issue["issue_number"], settings.trigger_label)
            labels = [*labels, settings.trigger_label]
            # Keep local sync in sync for the Issues UI "assigned" badge path.
            with db_session() as session:
                row = session.get(SyncedIssue, issue["id"])
                if row is not None:
                    row.labels = json.dumps(labels)

        if not labeled:
            results.append(
                {
                    "issue_id": issue["id"],
                    "ok": False,
                    "detail": "failed to add trigger label on GitHub",
                }
            )
            continue

        result = None
        if prefer_webhook:
            # Label delivery is async; wait briefly for webhook → create_and_dispatch.
            for _ in range(8):
                await asyncio.sleep(0.5)
                with db_session() as session:
                    existing = (
                        session.query(Task)
                        .filter(
                            Task.repository == issue["repository"],
                            Task.issue_number == issue["issue_number"],
                            Task.status.in_((TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.COMPLETED)),
                        )
                        .order_by(Task.id.desc())
                        .first()
                    )
                    if existing:
                        result = {
                            "task_id": existing.id,
                            "session_id": existing.droid_session_id,
                            "status": existing.status,
                            "via": "webhook",
                        }
                        break

        if result is None:
            try:
                dispatched = await create_and_dispatch_task(
                    repository=issue["repository"],
                    repository_url=issue["repository_url"],
                    issue_number=issue["issue_number"],
                    issue_title=issue["title"],
                    issue_body=issue["body"],
                    labels=labels,
                )
                result = {**dispatched, "via": "direct" if not prefer_webhook else "webhook_fallback"}
            except TaskCreateError as exc:
                results.append(
                    {
                        "issue_id": issue["id"],
                        "ok": exc.status_code == 200,
                        "detail": exc.message,
                        "task_id": exc.task_id,
                        "label": settings.trigger_label,
                    }
                )
                continue

        results.append(
            {
                "issue_id": issue["id"],
                "ok": True,
                "label": settings.trigger_label,
                **result,
            }
        )

    accepted = sum(1 for r in results if r.get("ok"))
    return {"detail": "processed", "accepted": accepted, "results": results}


@app.post("/api/webhooks/sync", response_model=None)
async def sync_github_webhooks() -> JSONResponse | dict[str, Any]:
    """Ensure trigger label + issues webhook exist on every registered repository."""
    settings = get_settings()
    if not (settings.public_base_url or "").strip():
        return JSONResponse(
            status_code=400,
            content={
                "detail": "PUBLIC_BASE_URL is not set; start a tunnel and set it so GitHub can reach /webhook",
            },
        )
    github = GitHubClient()
    with db_session() as session:
        repos = [r.full_name for r in session.query(Repository).all()]
    results = []
    for full_name in repos:
        automation = await _ensure_repo_automation(github, full_name)
        results.append({"repository": full_name, **automation})
    return {"detail": "synced", "public_base_url": settings.public_base_url, "results": results}


@app.get("/api/pulls")
async def list_synced_pulls() -> dict[str, Any]:
    """Return review-ready PRs, refreshing from GitHub for every registered repo."""
    with db_session() as session:
        repos = session.query(Repository).all()
        repo_ids = [r.id for r in repos]
        active_reviews = {
            (r.repository, r.pr_number)
            for r in session.query(ReviewTask).filter(ReviewTask.status.in_(TaskStatus.ACTIVE)).all()
        }

    for repo_id in repo_ids:
        result = await _sync_repository_pulls(repo_id)
        if result.get("error"):
            log_event(
                logger,
                logging.WARNING,
                "pulls.sync_skipped",
                repo_id=repo_id,
                error=result["error"],
            )

    with db_session() as session:
        pulls = (
            session.query(SyncedPullRequest)
            .order_by(SyncedPullRequest.repository.asc(), SyncedPullRequest.pr_number.desc())
            .all()
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for pull in pulls:
            payload = pull.to_dict()
            payload["assigned"] = (pull.repository, pull.pr_number) in active_reviews
            grouped.setdefault(pull.repository, []).append(payload)
        return {"repositories": [{"repository": repo, "pulls": items} for repo, items in grouped.items()]}


@app.post("/api/pulls/assign", response_model=None)
async def assign_pulls_to_droid(payload: AssignPullsRequest) -> JSONResponse | dict[str, Any]:
    """Launch a Droid review session on the selected pull requests."""
    with db_session() as session:
        pulls = session.query(SyncedPullRequest).filter(SyncedPullRequest.id.in_(payload.pull_ids)).all()
        snapshots = [p.to_dict() for p in pulls]

    if not snapshots:
        return JSONResponse(status_code=404, content={"detail": "no matching pull requests"})

    results = []
    for pull in snapshots:
        try:
            result = await start_pr_review(
                repository=pull["repository"],
                repository_url=pull.get("repository_url") or "",
                pr_number=pull["pr_number"],
                pr_url=pull.get("html_url") or "",
                pr_title=pull.get("title") or "",
            )
            results.append(
                {
                    "pull_id": pull["id"],
                    "ok": bool(result.get("started")),
                    "detail": result.get("detail", ""),
                    "review_id": result.get("review_id"),
                    "pr_number": pull["pr_number"],
                }
            )
            log_event(
                logger,
                logging.INFO,
                "droid.review_assigned",
                repo=pull["repository"],
                pr=pull["pr_number"],
                started=result.get("started"),
            )
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "droid.review_assign_failed",
                repo=pull["repository"],
                pr=pull["pr_number"],
                error=str(exc),
            )
            results.append(
                {
                    "pull_id": pull["id"],
                    "ok": False,
                    "detail": str(exc),
                }
            )

    accepted = sum(1 for r in results if r.get("ok"))
    return {"detail": "processed", "accepted": accepted, "results": results}


@app.get("/api/tasks")
async def list_tasks(page: int = 1, page_size: int = 10) -> dict[str, Any]:
    with db_session() as session:
        return recent_activity(session, page=page, page_size=page_size)


@app.post("/api/tasks/{task_id}/follow-up", response_model=None)
async def follow_up_task(task_id: int, payload: FollowUpRequest) -> JSONResponse | dict[str, Any]:
    """Send a follow-up prompt to the task's Droid session (SDK resume + send).

    Live-extend hook for interview demos — e.g. \"fix failing CI\" or
    \"add a regression test\".
    """
    from app.worker import finalize_fix_task

    droid = get_droid_client()
    if not droid.configured:
        return JSONResponse(
            status_code=503,
            content={"detail": "Droid is not configured (FACTORY_API_KEY missing)"},
        )

    with db_session() as session:
        task = session.get(Task, task_id)
        if not task:
            return JSONResponse(status_code=404, content={"detail": "task not found"})
        session_id = task.droid_session_id
        if not session_id:
            return JSONResponse(
                status_code=409,
                content={"detail": "task has no Droid session to follow up on"},
            )

    try:
        result = await droid.send_follow_up(
            session_id,
            payload.instruction,
            on_complete=partial(finalize_fix_task, task_id),
        )
    except DroidSessionBusyError:
        return JSONResponse(
            status_code=409,
            content={"detail": "the Droid session is still running a turn; retry when it finishes"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"follow-up failed: {exc}"},
        )

    with db_session() as session:
        db_task = session.get(Task, task_id)
        if db_task is not None:
            db_task.status = TaskStatus.RUNNING
            db_task.error = None

    log_event(
        logger,
        logging.INFO,
        "task.follow_up",
        task_id=task_id,
        session_id=session_id,
    )
    return {"detail": "accepted", "task_id": task_id, **result}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    with db_session() as session:
        stats = compute_metrics(session)
        activity = recent_activity(session, page=1, page_size=10)
    operator = operator_context(request)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "tasks": activity["tasks"],
            "activity": activity,
            "asset_version": asset_version(),
            "csrf_token": operator.csrf_token if operator else "",
            "auth_method": operator.method if operator else "",
        },
    )
