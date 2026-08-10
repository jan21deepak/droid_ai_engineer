"""FastAPI application: webhook intake, dashboard, metrics and health endpoints."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import __version__
from app.config import get_settings
from app.database import check_db_connectivity, db_session, init_db
from app.cursor_client import CursorClient
from app.github import GitHubClient, parse_issue_event, should_trigger, verify_signature
from app.logging_conf import log_event, setup_logging
from app.metrics import compute_metrics, recent_activity
from app.models import Repository, SyncedIssue, SyncedPullRequest, Task, TaskStatus
from app.repos import parse_repository_ref, resolve_agent_launch_config
from app.tasks import TaskCreateError, create_and_dispatch_task
from app.worker import worker_loop

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    init_db()
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_loop(stop_event))
    log_event(logger, logging.INFO, "app.started", version=__version__, agent_runtime="cursor-sdk")
    yield
    stop_event.set()
    await worker_task
    CursorClient.shutdown()


app = FastAPI(title="cursor-ai-engineer", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


class AddRepositoryRequest(BaseModel):
    url: str = Field(..., description="GitHub repository URL or owner/repo slug")
    cursor_environment: str = Field(
        default="",
        description="Named Cursor Cloud Agent environment for this repository",
    )
    starting_ref: str = Field(
        default="",
        description="Optional git ref used when no named environment is set",
    )


class UpdateRepositoryRequest(BaseModel):
    cursor_environment: str | None = Field(
        default=None,
        description="Named Cursor Cloud Agent environment for this repository",
    )
    starting_ref: str | None = Field(
        default=None,
        description="Optional git ref used when no named environment is set",
    )


class AssignIssuesRequest(BaseModel):
    issue_ids: list[int] = Field(..., min_length=1)


class AssignPullsRequest(BaseModel):
    pull_ids: list[int] = Field(..., min_length=1)


class FollowUpRequest(BaseModel):
    instruction: str = Field(
        ...,
        min_length=1,
        description="Follow-up prompt for an existing Cursor Cloud Agent",
    )


@app.get("/")
async def root():
    """Send browsers to the dashboard UI (JSON API discovery lives under /health)."""
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/health")
async def health():
    settings = get_settings()
    db_ok = check_db_connectivity()
    github_ok = await GitHubClient().check_connectivity()
    cursor_ok = await CursorClient().check_connectivity()

    if not db_ok:
        overall = "unhealthy"
        status_code = 503
    elif not github_ok or not cursor_ok:
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
                "cursor": "ok" if cursor_ok else ("unconfigured" if not settings.cursor_api_key else "error"),
            },
        },
    )


@app.post("/webhook")
async def webhook(request: Request):
    settings = get_settings()
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    event_type = request.headers.get("X-GitHub-Event", "")

    if not verify_signature(settings.github_webhook_secret, body, signature):
        log_event(logger, logging.WARNING, "webhook.signature_invalid", github_event=event_type)
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=422, content={"detail": "invalid JSON payload"})

    log_event(logger, logging.INFO, "webhook.received", github_event=event_type,
              action=payload.get("action"))

    if event_type == "ping":
        return {"detail": "pong"}
    if event_type != "issues":
        return {"detail": f"ignored event type: {event_type or 'unknown'}"}

    event = parse_issue_event(payload)
    if not event.repository or not event.issue_number:
        return JSONResponse(status_code=422, content={"detail": "malformed issues payload"})

    if not should_trigger(event, settings.trigger_label):
        log_event(logger, logging.INFO, "webhook.ignored", repo=event.repository,
                  issue=event.issue_number, action=event.action,
                  reason=f"no trigger (label '{settings.trigger_label}' required)")
        return {"detail": "ignored: trigger conditions not met"}

    try:
        result = await create_and_dispatch_task(
            repository=event.repository,
            repository_url=event.repository_url,
            issue_number=event.issue_number,
            issue_title=event.issue_title,
            issue_body=event.issue_body,
            labels=event.labels,
        )
    except TaskCreateError as exc:
        if exc.status_code == 200:
            return {"detail": exc.message, "task_id": exc.task_id}
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "task_id": exc.task_id},
        )

    return JSONResponse(status_code=202, content={"detail": "accepted", **result})


@app.get("/metrics")
async def metrics():
    with db_session() as session:
        return compute_metrics(session)


@app.get("/api/repositories")
async def list_repositories():
    with db_session() as session:
        repos = session.query(Repository).order_by(Repository.created_at.desc()).all()
        return {"repositories": [r.to_dict() for r in repos]}


@app.get("/api/cursor/environments")
async def list_cursor_environments():
    """Named Cursor Cloud Agent environments for the dashboard dropdown.

    Cursor has no public list-environments API, so names are discovered from
    recent agents plus any values already saved on registered repositories.
    """
    cursor = CursorClient()
    discovered = await cursor.list_environments() if cursor.configured else []
    by_name: dict[str, dict] = {
        item["name"]: {
            "name": item["name"],
            "repositories": list(item.get("repositories") or []),
            "source": item.get("source") or "cursor",
        }
        for item in discovered
        if item.get("name")
    }

    with db_session() as session:
        for repo in session.query(Repository).all():
            name = (repo.cursor_environment or "").strip()
            if not name:
                continue
            entry = by_name.setdefault(
                name,
                {"name": name, "repositories": [], "source": "saved"},
            )
            if repo.url and repo.url not in entry["repositories"]:
                entry["repositories"].append(repo.url)
            if repo.full_name:
                full = f"https://github.com/{repo.full_name}"
                if full not in entry["repositories"] and repo.full_name not in entry["repositories"]:
                    entry["repositories"].append(repo.full_name)

    environments = sorted(by_name.values(), key=lambda row: row["name"].lower())
    return {"environments": environments, "count": len(environments)}


@app.post("/api/repositories")
async def add_repository(payload: AddRepositoryRequest):
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
            cursor_environment=(payload.cursor_environment or "").strip(),
            starting_ref=(payload.starting_ref or "").strip()
            or (meta.get("default_branch") or "main")
            or "main",
        )
        session.add(repo)
        session.flush()
        log_event(
            logger,
            logging.INFO,
            "repository.added",
            repo=repo.full_name,
            environment=repo.cursor_environment or None,
        )
        return JSONResponse(status_code=201, content={"repository": repo.to_dict()})


@app.patch("/api/repositories/{repo_id}")
async def update_repository(repo_id: int, payload: UpdateRepositoryRequest):
    """Update Cursor environment / starting ref for a registered repository."""
    with db_session() as session:
        repo = session.get(Repository, repo_id)
        if not repo:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        if payload.cursor_environment is not None:
            repo.cursor_environment = payload.cursor_environment.strip()
        if payload.starting_ref is not None:
            repo.starting_ref = payload.starting_ref.strip()
        session.flush()
        log_event(
            logger,
            logging.INFO,
            "repository.updated",
            repo=repo.full_name,
            environment=repo.cursor_environment or None,
            starting_ref=repo.starting_ref or None,
        )
        return {"repository": repo.to_dict()}


@app.delete("/api/repositories/{repo_id}")
async def delete_repository(repo_id: int):
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


def _persist_synced_issues(full_name: str, repo_url: str, issues: list[dict]) -> int:
    """Replace the synced issues for a repository with the supplied open issues."""
    deduped: dict[int, dict] = {}
    for item in issues:
        number = int(item.get("number") or 0)
        if not number or (item.get("state") or "open") != "open":
            continue
        deduped[number] = item

    now = datetime.now(timezone.utc)
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


def _persist_synced_pulls(full_name: str, repo_url: str, pulls: list[dict]) -> int:
    """Replace synced open, non-draft PRs for a repository."""
    deduped: dict[int, dict] = {}
    for item in pulls:
        number = int(item.get("number") or 0)
        if not number or item.get("draft"):
            continue
        if (item.get("state") or "open") != "open":
            continue
        deduped[number] = item

    now = datetime.now(timezone.utc)
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


async def _sync_repository_pulls(repo_id: int) -> dict:
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


@app.post("/api/repositories/{repo_id}/sync-issues")
async def sync_repository_issues(repo_id: int):
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


@app.post("/api/repositories/{repo_id}/issues")
async def create_repository_issues(repo_id: int):
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
    candidates = [
        item
        for item in parent_issues
        if f"Source issue: {item.get('html_url')}" not in existing_bodies
    ][:5]
    if not candidates:
        return {
            "detail": "no_new_parent_issues",
            "repository": full_name,
            "parent": parent,
            "created": [],
            "synced_count": len(existing_issues),
        }

    created = []
    created_payloads: list[dict] = []
    try:
        for source in candidates:
            source_url = source.get("html_url") or (
                f"https://github.com/{parent}/issues/{source.get('number')}"
            )
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
async def list_synced_issues():
    with db_session() as session:
        issues = (
            session.query(SyncedIssue)
            .order_by(SyncedIssue.repository.asc(), SyncedIssue.issue_number.desc())
            .all()
        )
        active_keys = {
            (t.repository, t.issue_number)
            for t in session.query(Task).filter(
                Task.status.in_((TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.COMPLETED))
            )
        }
        grouped: dict[str, list] = {}
        for issue in issues:
            payload = issue.to_dict()
            payload["assigned"] = (issue.repository, issue.issue_number) in active_keys
            grouped.setdefault(issue.repository, []).append(payload)
        return {
            "repositories": [
                {"repository": repo, "issues": items} for repo, items in grouped.items()
            ]
        }


@app.post("/api/issues/assign")
async def assign_issues_to_cursor(payload: AssignIssuesRequest):
    settings = get_settings()
    github = GitHubClient()

    with db_session() as session:
        issues = (
            session.query(SyncedIssue)
            .filter(SyncedIssue.id.in_(payload.issue_ids))
            .all()
        )
        snapshots = [issue.to_dict() for issue in issues]

    if not snapshots:
        return JSONResponse(status_code=404, content={"detail": "no matching issues"})

    results = []
    for issue in snapshots:
        labels = []
        try:
            labels = json.loads(issue.get("labels") or "[]")
        except json.JSONDecodeError:
            labels = []
        if settings.trigger_label not in labels:
            labels = list(labels) + [settings.trigger_label]
            await github.add_issue_label(
                issue["repository"], issue["issue_number"], settings.trigger_label
            )

        try:
            result = await create_and_dispatch_task(
                repository=issue["repository"],
                repository_url=issue["repository_url"],
                issue_number=issue["issue_number"],
                issue_title=issue["title"],
                issue_body=issue["body"],
                labels=labels,
            )
            results.append({"issue_id": issue["id"], "ok": True, **result})
        except TaskCreateError as exc:
            results.append({
                "issue_id": issue["id"],
                "ok": exc.status_code == 200,
                "detail": exc.message,
                "task_id": exc.task_id,
            })

    accepted = sum(1 for r in results if r.get("ok"))
    return {"detail": "processed", "accepted": accepted, "results": results}


@app.get("/api/pulls")
async def list_synced_pulls():
    """Return review-ready PRs, refreshing from GitHub for every registered repo."""
    with db_session() as session:
        repos = session.query(Repository).all()
        repo_ids = [r.id for r in repos]

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
        grouped: dict[str, list] = {}
        for pull in pulls:
            payload = pull.to_dict()
            # Legacy field: previously meant "Cursor review agent active".
            payload["assigned"] = False
            grouped.setdefault(pull.repository, []).append(payload)
        return {
            "repositories": [
                {"repository": repo, "pulls": items} for repo, items in grouped.items()
            ]
        }


@app.post("/api/pulls/assign")
async def assign_pulls_to_bugbot(payload: AssignPullsRequest):
    """Trigger Cursor Bugbot on selected PRs (comment ``bugbot run``).

    Forge no longer starts Cursor Cloud review agents.
    """
    with db_session() as session:
        pulls = (
            session.query(SyncedPullRequest)
            .filter(SyncedPullRequest.id.in_(payload.pull_ids))
            .all()
        )
        snapshots = [p.to_dict() for p in pulls]

    if not snapshots:
        return JSONResponse(status_code=404, content={"detail": "no matching pull requests"})

    github = GitHubClient()
    results = []
    for pull in snapshots:
        try:
            requested = await github.request_bugbot_review(
                pull["repository"], pull["pr_number"]
            )
            results.append(
                {
                    "pull_id": pull["id"],
                    "ok": True,
                    "detail": "bugbot_requested" if requested else "bugbot_already_requested",
                    "pr_number": pull["pr_number"],
                }
            )
            log_event(
                logger,
                logging.INFO,
                "bugbot.assigned",
                repo=pull["repository"],
                pr=pull["pr_number"],
                requested=requested,
            )
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "github.bugbot_trigger_failed",
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
async def list_tasks(page: int = 1, page_size: int = 10):
    with db_session() as session:
        return recent_activity(session, page=page, page_size=page_size)


@app.post("/api/tasks/{task_id}/follow-up")
async def follow_up_task(task_id: int, payload: FollowUpRequest):
    """Send a follow-up prompt to the task's Cursor Cloud Agent (SDK resume + send).

    Live-extend hook for interview demos — e.g. \"fix failing CI\" or
    \"add a regression test\".
    """
    cursor = CursorClient()
    if not cursor.configured:
        return JSONResponse(
            status_code=503,
            content={"detail": "Cursor API is not configured"},
        )

    with db_session() as session:
        task = session.get(Task, task_id)
        if not task:
            return JSONResponse(status_code=404, content={"detail": "task not found"})
        agent_id = task.cursor_agent_id
        if not agent_id:
            return JSONResponse(
                status_code=409,
                content={"detail": "task has no Cursor agent to follow up on"},
            )

    try:
        result = await cursor.send_follow_up(agent_id, payload.instruction)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"follow-up failed: {exc}"},
        )

    with db_session() as session:
        db_task = session.get(Task, task_id)
        if db_task is not None:
            if result.get("run_id"):
                db_task.cursor_run_id = result["run_id"]
            db_task.status = TaskStatus.RUNNING
            db_task.error = None

    log_event(
        logger,
        logging.INFO,
        "task.follow_up",
        task_id=task_id,
        agent_id=agent_id,
        run_id=result.get("run_id"),
    )
    return {"detail": "accepted", "task_id": task_id, **result}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    with db_session() as session:
        stats = compute_metrics(session)
        activity = recent_activity(session, page=1, page_size=10)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "tasks": activity["tasks"],
            "activity": activity,
            "asset_version": asset_version(),
        },
    )
