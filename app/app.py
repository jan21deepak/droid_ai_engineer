"""FastAPI application: webhook intake, dashboard, metrics and health endpoints."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.acceptance_checks import recover_interrupted_check_runs, shutdown_check_runs
from app.analytics import fire_and_forget
from app.auth import AccessControlMiddleware, operator_credential_problem
from app.auth_routes import router as auth_router
from app.config import ACCESS_MODE_PROTECTED, effective_access_mode, get_settings
from app.database import check_db_connectivity, db_session, init_db
from app.delivery_routes import router as delivery_router
from app.droid_client import get_droid_client
from app.economics_routes import router as economics_router
from app.error_tracking import init_error_tracking
from app.evidence_export import router as evidence_router
from app.github import GitHubClient
from app.intake import router as intake_router
from app.logging_conf import log_event, setup_logging
from app.metrics import compute_metrics, recent_activity
from app.models import Repository, ReviewTask, SyncedIssue, SyncedPullRequest, Task, TaskStatus
from app.policy import OutwardAction, outward_decision, repository_decision
from app.publishing import recover_interrupted_publications
from app.recovery import recover_interrupted_tasks
from app.redaction import safe_error
from app.repository_routes import ensure_repo_automation, sync_repository_pulls
from app.repository_routes import router as repository_router
from app.review_routes import router as review_router
from app.run_control import runtime_watchdog
from app.run_routes import router as run_router
from app.tasks import TaskCreateError, create_and_dispatch_task
from app.tracing import RequestIDMiddleware
from app.ui_routes import router as ui_router
from app.ui_templates import STATIC_DIR, page_context, templates
from app.worker import start_pr_review, worker_loop

logger = logging.getLogger("app.api")


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
    recover_interrupted_check_runs()
    recover_interrupted_publications()
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_loop(stop_event))
    watchdog_task = asyncio.create_task(runtime_watchdog(stop_event))
    log_event(logger, logging.INFO, "app.started", version=__version__, agent_runtime="droid-sdk")
    yield
    stop_event.set()
    await worker_task
    await watchdog_task
    await shutdown_check_runs()
    await droid.shutdown()


app = FastAPI(title="droid-ai-engineer", version=__version__, lifespan=lifespan)
# Default-deny operator access; added first so request tracing wraps rejections too.
app.add_middleware(AccessControlMiddleware)
# Request tracing: every request gets an X-Request-ID echoed into logs.
app.add_middleware(RequestIDMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(auth_router)
app.include_router(intake_router)
app.include_router(repository_router)
app.include_router(run_router)
app.include_router(delivery_router)
app.include_router(economics_router)
app.include_router(evidence_router)
app.include_router(review_router)
app.include_router(ui_router)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 without echoing the rejected input.

    The default handler echoes each rejected value, which cannot be serialized
    for a non-finite number (``Infinity``/``NaN`` bodies would become a 500) and
    could reflect a pasted credential.
    """
    errors = [{"type": err.get("type"), "loc": list(err.get("loc", ())), "msg": err.get("msg")} for err in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


class AssignIssuesRequest(BaseModel):
    issue_ids: list[int] = Field(..., min_length=1)


class AssignPullsRequest(BaseModel):
    pull_ids: list[int] = Field(..., min_length=1)


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


@app.get("/api/droid/models")
async def list_droid_models() -> dict[str, Any]:
    """Model catalog for the dashboard dropdown (Factory Router = "auto")."""
    droid = get_droid_client()
    models = await droid.list_available_models() if droid.configured else []
    return {"models": models, "count": len(models)}


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


async def _label_issue(
    github: GitHubClient, issue: dict[str, Any], trigger_label: str, *, write: bool
) -> tuple[bool, list[str]]:
    """Apply the trigger label on GitHub when writes are permitted. Returns ``(ok, labels)``."""
    try:
        labels: list[str] = json.loads(issue.get("labels") or "[]")
    except json.JSONDecodeError:
        labels = []
    if not write:
        return True, labels
    await github.ensure_label(issue["repository"], trigger_label)
    if trigger_label in labels:
        return True, labels
    labeled = await github.add_issue_label(issue["repository"], issue["issue_number"], trigger_label)
    labels = [*labels, trigger_label]
    # Keep local sync in sync for the Issues UI "assigned" badge path.
    with db_session() as session:
        row = session.get(SyncedIssue, issue["id"])
        if row is not None:
            row.labels = json.dumps(labels)
    return labeled, labels


async def _wait_for_webhook_task(issue: dict[str, Any]) -> dict[str, Any] | None:
    """Label delivery is async; wait briefly for webhook → create_and_dispatch."""
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
                return {
                    "task_id": existing.id,
                    "session_id": existing.droid_session_id,
                    "status": existing.status,
                    "via": "webhook",
                }
    return None


@app.post("/api/issues/assign", response_model=None)
async def assign_issues_to_droid(payload: AssignIssuesRequest) -> JSONResponse | dict[str, Any]:
    """Dispatch selected issues to Droid, labeling them on GitHub when policy permits.

    Issues outside repository policy are rejected before any call. With GitHub
    writes permitted, the trigger label is applied; when ``PUBLIC_BASE_URL`` is
    configured, forge waits briefly for the webhook to create the task.
    Otherwise it dispatches the Droid session directly (same end state, with
    duplicate protection if the webhook also fires).
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
    results: list[dict[str, Any]] = []
    for issue in snapshots:
        access = repository_decision(issue["repository"])
        if not access.allowed:
            results.append(
                {"issue_id": issue["id"], "ok": False, "detail": f"repository not approved by policy: {access.reason}"}
            )
            continue
        # Labeling is a GitHub write. Without permission the work is still
        # dispatched locally; the label (and webhook path) is simply skipped.
        label_policy = outward_decision(OutwardAction.ISSUE_LABEL, issue["repository"])
        labeled, labels = await _label_issue(github, issue, settings.trigger_label, write=label_policy.allowed)

        if not labeled:
            results.append(
                {
                    "issue_id": issue["id"],
                    "ok": False,
                    "detail": "failed to add trigger label on GitHub",
                }
            )
            continue

        result = await _wait_for_webhook_task(issue) if prefer_webhook and label_policy.allowed else None

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
                via = "webhook_fallback" if prefer_webhook and label_policy.allowed else "direct"
                result = {**dispatched, "via": via}
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
                "label": settings.trigger_label if label_policy.allowed else None,
                **({} if label_policy.allowed else {"label_skipped": label_policy.reason}),
                **result,
            }
        )

    accepted = sum(1 for r in results if r.get("ok"))
    return {"detail": "processed", "accepted": accepted, "results": results}


@app.post("/api/webhooks/sync", response_model=None)
async def sync_github_webhooks() -> JSONResponse | dict[str, Any]:
    """Ensure trigger label + issues webhook exist on every registered repository.

    Installing webhooks is never a permitted outward action, so this is
    refused before any GitHub call; configure the hook in GitHub instead.
    """
    settings = get_settings()
    hook_policy = outward_decision(OutwardAction.WEBHOOK_INSTALL, "")
    if not hook_policy.allowed:
        return JSONResponse(status_code=403, content={"detail": f"webhook installation denied: {hook_policy.reason}"})
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
        automation = await ensure_repo_automation(github, full_name)
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
        result = await sync_repository_pulls(repo_id)
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

    results: list[dict[str, Any]] = []
    for pull in snapshots:
        access = repository_decision(pull["repository"])
        if not access.allowed:
            results.append(
                {"pull_id": pull["id"], "ok": False, "detail": f"repository not approved by policy: {access.reason}"}
            )
            continue
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
                    "detail": safe_error(exc),
                }
            )

    accepted = sum(1 for r in results if r.get("ok"))
    return {"detail": "processed", "accepted": accepted, "results": results}


@app.get("/api/tasks")
async def list_tasks(page: int = 1, page_size: int = 10) -> dict[str, Any]:
    with db_session() as session:
        return recent_activity(session, page=page, page_size=page_size)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    with db_session() as session:
        stats = compute_metrics(session)
        activity = recent_activity(session, page=1, page_size=10)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        page_context(request, stats=stats, tasks=activity["tasks"], activity=activity),
    )
