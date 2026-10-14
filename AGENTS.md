# AGENTS.md

Instructions for autonomous agents (Droid and humans) working in this repository.

## What this is

Droid Forge is a FastAPI service that turns labeled GitHub issues into reviewed pull
requests using local Droid SDK sessions. When an issue is labeled `Droid-complete`
(or assigned via the dashboard), the service clones the target repo, runs a Droid
agent to fix it and records the resulting commit as a local candidate. Agent success
never pushes, opens a PR or merges; publishing is a separate policy-gated step
(`app/publishing.py`). PRs assigned for review get a fresh Droid review session
restricted to Read/Grep/Glob over the exact base..head diff.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt            # runtime deps, exact pins
pip install -r requirements-dev.txt        # lint/type/test tooling
pre-commit install                         # quality gates on commit
cp .env.example .env                       # then fill in tokens (see .env.example)
```

The service boots without API keys (reports `degraded` on `/api/health`); only the
webhook trigger flow needs real tokens and a tunnel. Operator access defaults to
`FORGE_ACCESS_MODE=protected` and needs `FORGE_OPERATOR_TOKEN` (32+ chars);
`FORGE_ACCESS_MODE=local_dev` explicitly disables sign-in for local work. See
"Operator access" in `README.md`.

## Build / run / test commands

| Task | Command |
|---|---|
| Run the service | `.venv/bin/uvicorn app.app:app --host 127.0.0.1 --port 8000 --no-proxy-headers` |
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

1. Start the service: `.venv/bin/uvicorn app.app:app --host 127.0.0.1 --port 8000 --no-proxy-headers`.
2. `curl -s http://localhost:8000/health` must return `200` with
   `checks.database == "ok"`. Operator-only `/api/health` adds github/droid
   (they may read `unconfigured` without tokens).
3. `/metrics` (with `Authorization: Bearer <operator token>`, or in `local_dev`) must return JSON totals.
4. Open `http://localhost:8000/dashboard` — sign in at `/login`; the dashboard must render.
5. To exercise the full issue-to-PR pipeline, see
   `tests/integration/test_issue_to_pr_flow.py` (the same flow, stubbed externals).

## Project layout

- `app/app.py` — FastAPI app: dashboard, repositories, issues APIs
- `app/run_models.py` / `app/attempts.py` — `RunAttempt` per-turn rows, lifecycle and usage storage; `app/run_routes.py` — task/review/attempt detail, follow-up and recovery APIs; `app/recovery.py` — restart recovery (marks orphans interrupted, never resumes) and explicit continuation
- `app/auth.py` / `app/auth_routes.py` — default-deny operator access middleware, sessions, CSRF/Origin/Host checks, login/logout
- `app/intake.py` — HMAC-signed `/webhook` intake; `app/policy.py` — repository approval policy
- `app/worker.py` — fix/review launch and attempt-bound finalization
- `app/candidates.py` — local candidate recording; `app/review_policy.py` — restricted review config, diff checks, verdict parsing; `app/publishing.py` — explicit publish of an approved candidate SHA (push, remote-head check, draft PR; never called automatically)
- `app/delivery.py` — SHA-bound readiness and operator approval; `app/delivery_models.py` — check runs, approvals, publications; `app/acceptance_checks.py` / `app/check_config.py` — app-run `FORGE_ACCEPTANCE_CHECKS`; `app/candidate_review.py` — restricted pre-publication review; `app/delivery_git.py` — clean checkouts, workspace recheck, push; `app/task_reservations.py` — in-process per-task reservations keeping admission/evidence/approval/finalization out of an in-flight publication; `app/remote_ci.py` — read-only GitHub CI observation; `app/review_outcomes.py` — AI verdict vs GitHub review outcome; `app/delivery_routes.py` — delivery APIs
- `app/overview.py` — executive overview stages and counts (`/api/overview`); `app/ui_routes.py` / `app/ui_templates.py` — `/tasks/{id}` and `/reviews/{id}` pages; `app/review_routes.py` — review list and assign-by-number; `app/static/` — plain JS over the JSON APIs (no build step)
- `app/droid_client.py` — Droid SDK wrapper: workspaces, fix/review sessions, resume
- `app/github.py` — GitHub REST client: comments, PRs, reviews, webhook signature
- `app/models.py` — SQLAlchemy `Task` / `ReviewTask` / `Repository` models
- `app/metrics.py` — agent activity indicators, legacy assumption-based estimates and daily series; `app/economics.py` / `app/economics_routes.py` — outcome economics and user-entered effort/assumptions; `app/evidence_export.py` — selected-task evidence export
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
  `tests/conftest.py` — don't touch the real `data/tasks.db` in tests. The legacy
  `client` fixture runs in explicit `local_dev` mode; use `protected_client` for
  access-control behavior.
- Every commit message should explain *why*; PRs use `.github/pull_request_template.md`.
- Debt markers must link an issue: `TODO(#123) ...` — bare `TODO` fails `scripts/check_todos.py`.

## Dependency policy

Runtime dependencies in `requirements.txt` are exact-pinned (`==`) to the tested
versions. Renovate opens update PRs and enforces a 7-day `minimumReleaseAge` —
do not hand-bump pins without running the full test suite.
