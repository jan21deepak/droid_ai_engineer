# AGENTS.md

Instructions for autonomous agents (Droid and humans) working in this repository.

## What this is

Droid Forge is a FastAPI service that turns labeled GitHub issues into reviewed pull
requests using local Droid SDK sessions. When an issue is labeled `Droid-complete`
(or assigned via the dashboard), the service clones the target repo, runs a Droid
agent to fix it, opens a same-repo PR, runs a second Droid review session, and posts
results back to GitHub.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt            # runtime deps, exact pins
pip install -r requirements-dev.txt        # lint/type/test tooling
pre-commit install                         # quality gates on commit
cp .env.example .env                       # then fill in tokens (see .env.example)
```

The service boots without API keys (reports `degraded` on `/health`); only the
webhook trigger flow needs real tokens and a tunnel.

## Build / run / test commands

| Task | Command |
|---|---|
| Run the service | `.venv/bin/uvicorn app.app:app --host 0.0.0.0 --port 8000` |
| Run the service in Docker | `docker compose up --build` |
| Run all tests | `.venv/bin/python -m pytest -q` |
| Run tests with coverage (threshold enforced in `pyproject.toml`) | `.venv/bin/python -m pytest --cov=app` |
| Lint + auto-fix | `ruff check app/ tests/ scripts/ --fix` |
| Format | `ruff format app/ tests/ scripts/` |
| Type check (strict) | `mypy app/` |
| Dead code scan | `vulture app/ --min-confidence 80` |
| File-size guard (max 1000 lines) | `python scripts/check_file_size.py` |
| Debt-marker guard | `python scripts/check_todos.py` |
| Regenerate committed API schema | `.venv/bin/python scripts/export_openapi.py` |

There is no separate build step; the service runs from source.

## Verification workflow

Before declaring any change done: run `ruff check`, `ruff format --check`,
`mypy app/`, and `pytest -q` — all must pass. CI runs the same set (see
`.github/workflows/ci.yml`) plus CodeQL, gitleaks, and jscpd.

## Interactive QA

1. Start the service: `.venv/bin/uvicorn app.app:app --port 8000`.
2. `curl -s http://localhost:8000/health` must return `200` with
   `checks.database == "ok"` (github/droid may read `unconfigured` without tokens).
3. `curl -s http://localhost:8000/metrics` must return JSON totals.
4. Open `http://localhost:8000/dashboard` — the dashboard must render.
5. To exercise the full issue-to-PR pipeline, see
   `tests/integration/test_issue_to_pr_flow.py` (the same flow, stubbed externals).

## Project layout

- `app/app.py` — FastAPI app: `/webhook`, dashboard, repositories, issues, follow-up APIs
- `app/worker.py` — background finalization (fix/review), PR state reconciliation, restart recovery
- `app/droid_client.py` — Droid SDK wrapper: workspaces, fix/review sessions, resume
- `app/github.py` — GitHub REST client: comments, PRs, reviews, webhook signature
- `app/models.py` — SQLAlchemy `Task` / `ReviewTask` / `Repository` models
- `app/metrics.py` — KPI / ROI / daily-series computation
- `tests/` — unit tests; `tests/integration/` — cross-layer pipeline test

## Conventions

- Python 3.12+, stdlib style with `from __future__ import annotations` where already present.
- Naming: `snake_case` for functions/variables/modules, `PascalCase` for classes,
  `UPPER_SNAKE` for module constants — enforced by ruff's pep8-naming rules (`N`).
- Status values are plain strings on the `TaskStatus` constants class (`app/models.py`), not an enum.
- Logging goes through `log_event(logger, level, "dotted.event.name", **context)`
  from `app/logging_conf.py`; timestamps are SGT (UTC+8). Never log secrets.
- Errors from external calls surface as `DroidRunError` / `httpx.HTTPStatusError`;
  finalize paths log-and-continue rather than crash the worker.
- Tests set up isolated per-test SQLite DBs via the autouse `clean_db` fixture in
  `tests/conftest.py` — don't touch the real `data/tasks.db` in tests.
- Every commit message should explain *why*; PRs use `.github/pull_request_template.md`.
- Debt markers must link an issue: `TODO(#123) ...` — bare `TODO` fails `scripts/check_todos.py`.

## Dependency policy

Runtime dependencies in `requirements.txt` are exact-pinned (`==`) to the tested
versions. Renovate opens update PRs and enforces a 7-day `minimumReleaseAge` —
do not hand-bump pins without running the full test suite.
