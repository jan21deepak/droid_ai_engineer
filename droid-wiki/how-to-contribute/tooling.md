# Tooling

The toolchain is deliberately thin: Python plus pip, Docker, the droid CLI, `gh`, and pytest. There is no build step beyond `pip install` and `docker build`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins minimums, and the list is short: `fastapi`, `uvicorn[standard]`, `sqlalchemy` (2.x), `httpx`, `python-dotenv`, `pydantic-settings`, `jinja2`, `droid-sdk`, and the test-only `pytest`, `pytest-asyncio`, `respx`. Python 3.12 is the floor (the Docker image is `python:3.12-slim`); newer interpreters work, and there is no lock file or hash pinning.

## Runtime

```bash
uvicorn app.app:app --host 0.0.0.0 --port 8000
```

One process serves the API, renders the dashboard, and runs the embedded worker loop. Logs go to stdout in the structured format from `app/logging_conf.py`; there is no separate log file. `curl -s http://localhost:8000/health` is the quickest smoke check.

## Docker

`docker compose up --build` is the full deployment. What the two files do:

- `Dockerfile` starts from `python:3.12-slim`, apt-installs git and curl (git for workspace clones, curl to install the Droid CLI), installs the CLI with `curl -fsSL https://app.factory.ai/cli | sh`, pip-installs requirements, copies `app/`, and runs as a non-root `appuser`. The healthcheck curls `/health` every 30 seconds.
- `docker-compose.yml` publishes port 8000, loads `.env`, and forces `DATABASE_URL=sqlite:////data/tasks.db` and `WORKSPACE_ROOT=/data/workspace`. Two named volumes matter: `droid_data` holds the database and workspace clones, and `droid_home` holds `/home/appuser/.factory`, the droid home where sessions persist. Both survive container restarts so interrupted runs resume. `restart: unless-stopped` is set.

Inside the container, always set `FACTORY_API_KEY`; the CLI-auth fallback is for local runs.

## The droid CLI

`droid-sdk` (the Python package in `requirements.txt`) drives the `droid` CLI as a subprocess, so the CLI must exist wherever the app runs. Install options are the curl installer above, Homebrew, or npm (see the Droid CLI docs linked from `README.md`). Verify with `droid --version`. Authentication is either `FACTORY_API_KEY` (what the SDK reads) or the CLI's own login when `DROID_ALLOW_CLI_AUTH=true` and the key is empty. Sessions, settings, and transcripts persist under `~/.factory`; see [Debugging](debugging.md) for reading transcripts.

## gh

The GitHub CLI is used two ways. `scripts/create_superset_issues.sh` requires it (authenticated with write access) to create labels and issues. It is also the fastest way to inspect GitHub state during debugging: `gh pr view`, `gh issue view`, and `gh api repos/<owner>/<repo>/hooks` for webhook configuration. The runbook lives in [Debugging](debugging.md).

## pytest

```bash
pytest -q
```

Configured entirely by `pytest.ini` (`asyncio_mode = auto`, `testpaths = tests`) plus the fixtures in `tests/conftest.py`. The suite is offline, 85 tests, and needs no network, tokens, or running services. See [Testing](testing.md).

## scripts/create_superset_issues.sh

A seeding helper for the demo target repo. Two environment variables control it: `REPO` (default `jan21deepak/superset`) and `LABEL` (default `Droid-complete`). It creates the trigger label if missing, then two sample issues with problem statements, acceptance criteria, and a standard instruction block ("make only the requested changes, run tests, open a PR"). Use it as a template for seeding your own sandbox repo during end-to-end validation.

## The architecture diagram

`docs/architecture.mmd` is the Mermaid source; `docs/architecture.png` is the rendered image embedded in `README.md`. Regenerate with:

```bash
npx -p @mermaid-js/mermaid-cli mmdc \
  -i docs/architecture.mmd \
  -o docs/architecture.png \
  -b "#0b1220" -w 1500 -s 1
```

Keep the `.mmd` file and the PNG in sync; the README embeds the PNG, not the source.

## What is not wired in

There is no linter, no formatter, no pre-commit hook, and no CI configuration in the repository. Nothing runs automatically on commit or push. Style is enforced by review against the conventions in [Patterns and conventions](patterns-and-conventions.md): standard Python, one module per concern, short docstrings on public functions. If you introduce a linter or formatter, keep it as a separate, optional step so the runtime path and the test suite stay untouched.
