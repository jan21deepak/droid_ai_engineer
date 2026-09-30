# API

Droid Forge serves one FastAPI application, defined in `app/app.py`, with three kinds of routes. `POST /webhook` is the GitHub-facing entry point: it verifies the webhook signature and turns labeled `issues` events into tasks. The JSON endpoints under `/api/` back the dashboard UI and cover repository registration, issue and pull request sync, assignment, task history, and the Droid model catalog. Operator routes (`/health`, `/metrics`, `/dashboard`) report service state and aggregate results. The dashboard JavaScript calls the same JSON endpoints, so anything clickable in the UI is also scriptable. Everything runs inside the single uvicorn process, alongside the embedded worker loop; there is no separate API tier.

## Endpoint groups

Every route is documented in [REST endpoints](rest-endpoints.md), grouped as follows:

- [Health and metrics](rest-endpoints.md#health-and-metrics): liveness and dependency checks, aggregate KPIs.
- [Repository management](rest-endpoints.md#repository-management): register, update, and remove repositories.
- [Issue sync and assignment](rest-endpoints.md#issue-sync-and-assignment): pull open issues locally and dispatch fix sessions.
- [Pull request sync and review assignment](rest-endpoints.md#pull-request-sync-and-review-assignment): list open PRs and launch review sessions.
- [Tasks and follow-up](rest-endpoints.md#tasks-and-follow-up): activity history and follow-up prompts to existing sessions.
- [Droid model catalog](rest-endpoints.md#droid-model-catalog): models available to Droid sessions.
- [Webhook intake](rest-endpoints.md#webhook-intake): GitHub webhook for `issues` events, plus webhook installation.
- [Dashboard](rest-endpoints.md#dashboard): the HTML UI and its redirect.

## Related pages

- [Getting started](../overview/getting-started.md) to run the service and dispatch a first task.
- [Configuration](../reference/configuration.md) for the environment variables these endpoints read.
- [Issue to PR](../features/issue-to-pr.md) for the workflow the assignment endpoints drive.
- [Security](../security.md) for the webhook signature check and what the JSON surface does not authenticate.
