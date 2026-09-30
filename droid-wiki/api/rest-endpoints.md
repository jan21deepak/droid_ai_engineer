# REST endpoints

All routes are declared in `app/app.py`. Request bodies are validated by Pydantic models declared alongside the routes (see [Request body models](#request-body-models)). The JSON surface has no authentication of its own: the webhook is protected by its HMAC signature, and the rest trusts the network it is bound to (see [Security](../security.md)).

## Writes to GitHub vs local state

| GitHub effect | Endpoints |
|---|---|
| Writes immediately | `POST /api/repositories` (trigger label, issues webhook), `POST /api/repositories/{repo_id}/issues`, `POST /api/issues/assign` (trigger label), `POST /api/webhooks/sync` |
| Writes later, through an agent run | `POST /webhook`, `POST /api/issues/assign`, `POST /api/pulls/assign`, `POST /api/tasks/{task_id}/follow-up` (branch push, PR, review, comment happen in the session) |
| Reads GitHub, writes local rows only | `POST /api/repositories/{repo_id}/sync-issues`, `GET /api/pulls` |
| Local state only | `GET` routes, `PATCH`/`DELETE /api/repositories/{repo_id}`, `/health`, `/metrics`, `/dashboard` |

## Health and metrics

| Endpoint | Purpose | Response notes |
|---|---|---|
| `GET /` | Redirect browsers to the dashboard | 307 to `/dashboard` |
| `GET /health` | Report service state | `status` is `healthy` (200), `degraded` (200, GitHub or Droid check down) or `unhealthy` (503, database down). Body includes `application`, `version`, and per-check `database` / `github` / `droid` values of `ok`, `unconfigured` or `error` |
| `GET /metrics` | Aggregate KPIs over all tasks and reviews | Built by `compute_metrics` in `app/metrics.py`: totals, `success_rate`, runtimes, credits, cost and productivity estimates, engineering KPIs, and daily buckets |

## Repository management

| Endpoint | Purpose | Request | Codes |
|---|---|---|---|
| `GET /api/repositories` | List registered repositories, newest first | none | 200 |
| `POST /api/repositories` | Register a repository and ensure its automation | `AddRepositoryRequest` | 201 created (body includes the repository and an `automation` report), 200 already registered, 422 invalid URL, 404 not found on GitHub, 502 GitHub request failed |
| `PATCH /api/repositories/{repo_id}` | Update per-repo model, setup command, starting ref | `UpdateRepositoryRequest` | 200, 404 |
| `DELETE /api/repositories/{repo_id}` | Remove a repository and its synced issues and PRs | none | 200, 404 |

On registration the service fetches repository metadata from GitHub, stores the row, ensures the trigger label exists on the repo, and when `PUBLIC_BASE_URL` is set installs or updates an issues webhook pointing at `{PUBLIC_BASE_URL}/webhook` (helper `_ensure_repo_automation`). An empty `starting_ref` falls back to the repository's default branch. Deletion only touches local rows; the label and webhook stay on GitHub.

## Issue sync and assignment

| Endpoint | Purpose | Request | Codes |
|---|---|---|---|
| `POST /api/repositories/{repo_id}/sync-issues` | Replace local synced issues with the repo's open issues (PRs excluded) and refresh synced PRs | none | 200, 404 repository not found, 502 GitHub failure |
| `POST /api/repositories/{repo_id}/issues` | Copy up to five new open issues from a fork's parent into the fork, creating real GitHub issues | none | 200 (`created` or `no_new_parent_issues`), 404, 422 not a fork, 502 |
| `GET /api/issues` | Group synced issues by repository, each with an `assigned` flag (a queued, running or completed task exists) | none | 200 |
| `POST /api/issues/assign` | Add the trigger label on GitHub and dispatch fix sessions | `AssignIssuesRequest` | 200 with per-issue results, 404 no matching issues |

Notes on assign: with `PUBLIC_BASE_URL` set, the endpoint labels the issue and waits briefly (eight polls at 0.5 seconds) for the webhook to create the task, reporting `via: "webhook"`; without it, dispatch is direct (`via: "direct"`). A duplicate issue returns `ok: true` with `detail: "duplicate: task already exists"` and the existing `task_id`. Parent-issue import matches already-imported issues by the `Source issue:` URL line in issue bodies.

## Pull request sync and review assignment

| Endpoint | Purpose | Request | Codes |
|---|---|---|---|
| `GET /api/pulls` | Refresh synced PRs (open, non-draft) from GitHub for every registered repo, then list them grouped by repository with an `assigned` flag | none | 200 |
| `POST /api/pulls/assign` | Start a Droid review session per selected PR | `AssignPullsRequest` | 200 with per-PR results, 404 no matching PRs |

Review assignment creates a `ReviewTask` row and launches the review session (`start_pr_review` in `app/worker.py`); the GitHub review is posted when the session finishes. An in-flight launch for the same PR returns `started: false` with `detail: "already_running"`.

## Tasks and follow-up

| Endpoint | Purpose | Request | Codes |
|---|---|---|---|
| `GET /api/tasks` | Paginated fix and review activity, newest first | query `page` (default 1), `page_size` (default 10, capped at 100) | 200 |
| `POST /api/tasks/{task_id}/follow-up` | Resume the task's Droid session with a new instruction | `FollowUpRequest` | 200 accepted (task flips back to `running`, error cleared), 404 task not found, 409 no session attached or a turn still in flight, 503 Droid not configured, 502 other failures |

## Droid model catalog

| Endpoint | Purpose | Response notes |
|---|---|---|
| `GET /api/droid/models` | List models available to Droid sessions (dashboard dropdown) | `models` array of `{id, display_name, provider}` plus `count`; disabled models filtered; empty list when Droid is not configured |

## Webhook intake

`POST /webhook` accepts GitHub webhook deliveries. It reads `X-Hub-Signature-256` and `X-GitHub-Event`, verifies the HMAC signature with `verify_signature` in `app/github.py` before parsing, and only `issues` events are considered. Trigger conditions (`should_trigger` in `app/github.py`): the issue was opened with the trigger label present, or the trigger label was just added. Accepted deliveries dispatch a task in the background and return immediately, since GitHub kills webhook deliveries after roughly ten seconds.

| Case | Response |
|---|---|
| Signature mismatch | 401 |
| Body is not JSON, or `issues` payload lacks repository or issue number | 422 |
| `ping` event | 200, `{"detail": "pong"}` |
| Other event types, or trigger conditions not met | 200 with an `ignored` detail |
| Duplicate task for the issue (queued, running or completed) | 200 with the existing `task_id` |
| Dispatched | 202 with `task_id`, `status: "queued"`, `session_id` (null until the background launcher links the session) |

`POST /api/webhooks/sync` re-runs automation setup for every registered repository: it ensures the trigger label and the issues webhook on each one. It returns 400 when `PUBLIC_BASE_URL` is unset (GitHub cannot reach `/webhook`), and 200 with a per-repository `results` array otherwise. This one writes directly to GitHub.

## Dashboard

| Endpoint | Purpose |
|---|---|
| `GET /dashboard` | Server-rendered HTML page (Jinja2) with metrics and the first page of activity |
| `GET /static/*` | Dashboard assets, served with a cache-busting version token |

## Request body models

All five models are declared in `app/app.py`.

| Model | Used by | Fields |
|---|---|---|
| `AddRepositoryRequest` | `POST /api/repositories` | `url` (required), `droid_model` (default `""`, empty means the global `DROID_MODEL`), `setup_command` (default `""`), `starting_ref` (default `""`) |
| `UpdateRepositoryRequest` | `PATCH /api/repositories/{repo_id}` | `droid_model`, `setup_command`, `starting_ref`, all optional; omit a field (null) to leave it unchanged |
| `AssignIssuesRequest` | `POST /api/issues/assign` | `issue_ids`: synced-issue ids, at least one |
| `AssignPullsRequest` | `POST /api/pulls/assign` | `pull_ids`: synced-PR ids, at least one |
| `FollowUpRequest` | `POST /api/tasks/{task_id}/follow-up` | `instruction` (required, non-empty follow-up prompt) |
