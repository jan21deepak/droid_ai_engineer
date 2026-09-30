# Deployment

Droid Forge deploys as one container. The image is defined in `Dockerfile`; `docker-compose.yml` builds it, loads secrets from `.env`, maps the port, and mounts two named volumes. A single uvicorn process serves the API and runs the embedded worker loop, so there is no second process to operate.

## What the image contains

- Base image `python:3.12-slim`.
- `git`, `curl` and ca-certificates installed with apt. `git` is needed for workspace clones and branch pushes; `curl` installs the CLI and backs the container healthcheck.
- The droid CLI, installed during the build with `curl -fsSL https://app.factory.ai/cli | sh` and verified with `droid --version`. The `droid-sdk` Python package drives this CLI as a subprocess, so the CLI must be present in the image.
- Python dependencies from `requirements.txt` and the `app/` package, under `/srv`.
- A non-root `appuser` with a home directory. The service runs as this user, with `HOME=/home/appuser` and `/home/appuser/.local/bin` prepended to `PATH`.

The image sets `DATABASE_URL=sqlite:////data/tasks.db` (an absolute path under `/data`) and `WORKSPACE_ROOT=/data/workspace`, exposes port 8000, and starts with `uvicorn app.app:app --host 0.0.0.0 --port 8000`.

## Healthcheck

The `Dockerfile` declares `HEALTHCHECK`: `curl -sf http://localhost:8000/health` every 30 seconds, with a 5 second timeout, a 10 second start period, and 3 retries. `/health` returns 503 only when the database check fails, so an unhealthy container means the SQLite file or its volume is broken. GitHub or Droid connectivity failures degrade the response body but still return 200; see [How to monitor](how-to-monitor.md).

## The two volumes

| Volume | Mount | Holds | Why it matters |
|---|---|---|---|
| `droid_data` | `/data` | SQLite database (`/data/tasks.db`) and per-task workspace clones (`/data/workspace`) | Task history, synced issues and PRs, and in-flight workspaces |
| `droid_home` | `/home/appuser/.factory` | The droid CLI's sessions and settings | Droid sessions persist here, which is what makes them resumable |

The second volume is easy to overlook. On startup, `recover_interrupted_tasks` in `app/worker.py` resumes sessions orphaned by a restart via `Session.resume` (`app/droid_client.py`). If `.factory` is not persisted, session files vanish with the container and recovery marks those rows failed (`recovery.session_lost`).

`docker-compose.yml` builds the image from the repo root, loads `.env` with `env_file`, repeats the two environment defaults, maps `8000:8000`, and sets `restart: unless-stopped`.

## Running without Docker

The README's local option is a virtualenv plus uvicorn:

```bash
pip install -r requirements.txt
mkdir -p data
.venv/bin/uvicorn app.app:app --host 0.0.0.0 --port 8000
```

Outside Docker the defaults from `app/config.py` apply: `DATABASE_URL=sqlite:///./data/tasks.db` and `WORKSPACE_ROOT=./data/workspace`, both relative to the working directory. See [Getting started](overview/getting-started.md).

## Droid auth inside Docker

`DROID_ALLOW_CLI_AUTH=true` tells the SDK to use the local droid CLI's own login when `FACTORY_API_KEY` is empty. That option is for local runs; the comment in `app/config.py` is explicit that in Docker you should always set `FACTORY_API_KEY`. There is no interactive CLI login inside the container, so a compose deployment needs the key in `.env`. The full variable list is in [Configuration](reference/configuration.md).

## Schema note

v2.0 renamed the task columns (for example `cursor_agent_id` became `droid_session_id`). The lightweight migration in `app/database.py` only adds columns that are missing from existing tables (`ALTER TABLE ... ADD COLUMN`); it does not rename or rewrite old data. Existing v1 SQLite files are not migrated: point `DATABASE_URL` at a fresh database, which in practice means starting with a fresh `droid_data` volume rather than reusing one from v1.
