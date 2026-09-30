# Getting started

## Prerequisites

- Python 3.12 or newer.
- The `droid` CLI on `PATH`, or a `FACTORY_API_KEY`. The Python SDK drives the CLI as a subprocess.
- `git`, for workspace clones and branch pushes.
- A GitHub personal access token with `repo` scope (add `workflow` if target repos have workflows).

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies are declared in `requirements.txt`. The agent runtime comes from the `droid-sdk` package, which is installed with the rest.

## Configure

Copy the example environment file and fill in the required values:

```bash
cp .env.example .env
```

At minimum set `GITHUB_TOKEN` and one of `FACTORY_API_KEY` or `DROID_ALLOW_CLI_AUTH=true`. Settings load from `.env` through `app/config.py`. The full list is in [Configuration](../reference/configuration.md).

## Run

```bash
uvicorn app.app:app --host 127.0.0.1 --port 8000
```

On startup the lifespan handler initializes the database, recovers interrupted tasks, and starts the worker loop. Check readiness:

```bash
curl -s http://127.0.0.1:8000/health
```

The response reports `database`, `github`, and `droid` separately. `droid` reads `ok` when a session can be opened, `unconfigured` when no key is set and CLI auth is off.

## First task

1. Open the dashboard at `http://127.0.0.1:8000/dashboard`.
2. Register a repository by URL. The service ensures the `Droid-complete` label exists on it.
3. Sync issues, then click **Assign to Droid** on an issue, or add the `Droid-complete` label directly on GitHub.
4. Watch the task move from `queued` to `running`, then to `completed` once the agent pushes and the PR opens.

With no `PUBLIC_BASE_URL` set, assignment dispatches the Droid session directly instead of waiting for a webhook. Set `PUBLIC_BASE_URL` to a URL GitHub can reach when you want webhook-driven intake.

## Test

```bash
pytest -q
```

The suite runs against a temporary SQLite database and stubs the Droid SDK and GitHub client. See [Testing](../how-to-contribute/testing.md).

## Docker

```bash
cp .env.example .env   # fill in the same values
docker compose up --build
```

The image installs the droid CLI and persists `/data` (database and workspaces) and `/home/appuser/.factory` (sessions). See [Deployment](../deployment.md).
