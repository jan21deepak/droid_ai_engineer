# How to monitor

## Where logs go

`setup_logging` in `app/logging_conf.py` installs a single stream handler on stdout, so logs go wherever the process's stdout goes (Docker's log driver, or your terminal). Lines are single-line `key=value` records:

    <SGT timestamp> | LEVEL | logger name | event name | field=value field=value

Timestamps are ISO-8601 in Singapore time with millisecond precision. `LOG_LEVEL` sets the root level, and uvicorn's access log is demoted to WARNING so agent events dominate the stream.

## log_event and the keyword rule

Structured events are emitted with `log_event(logger, level, event, **ctx)` in `app/logging_conf.py`. The context fields become the trailing `key=value` pairs. Because `logger`, `level` and `event` are the function's own parameter names, a context key named `event`, `logger` or `level` raises a `TypeError` (duplicate keyword argument). Never name a context field any of those three; use `event_name` or similar if you need to carry such a value.

## Events to watch

| Event | Source | Meaning |
|---|---|---|
| `task.created` | `app/tasks.py` | A task row was persisted for an issue (fields: `task_id`, `repo`, `issue`) |
| `droid.session_started` | `app/droid_client.py` | A fix session opened (fields: `session_id`, `task_id`, `repository`, `workspace`, `model`) |
| `droid.turn_finished` | `app/droid_client.py` | A fix, review, follow-up or recovery turn ended (fields: `session_id`, `kind`, `success`, `subtype`, `events`, `duration_seconds`); failures log at ERROR |
| `task.completed` | `app/worker.py` | A fix run succeeded (fields: `task_id`, `pr`, `runtime_seconds`) |
| `review.completed` | `app/worker.py` | A review finished and its verdict was handled (fields: `review_id`, `repo`, `pr`, `verdict`, `posted`) |
| `pr.state_changed` | `app/worker.py` | The reconciliation loop saw a PR move to `open`, `closed` or `merged` (fields: `task_id`, `pr`, `state`) |
| `recovery.dispatch_lost`, `recovery.session_lost`, `recovery.completed` | `app/worker.py` | Startup recovery of runs orphaned by a service restart |

Failure signals worth alerting on: `task.failed`, `review.failed`, `webhook.signature_invalid`, `droid.not_configured`, `droid.session_start_failed`, and `worker.iteration_error`.

## The /health checks

`GET /health` probes three dependencies and returns one overall status:

| Check | What it probes | Values |
|---|---|---|
| `database` | SQLite connectivity (`check_db_connectivity` in `app/database.py`) | `ok` / `error` |
| `github` | `GET /rate_limit` with the PAT (`GitHubClient.check_connectivity` in `app/github.py`) | `ok` / `unconfigured` (no `GITHUB_TOKEN`) / `error` |
| `droid` | Model listing via `list_models` (`DroidClient.check_connectivity` in `app/droid_client.py`) | `ok` / `unconfigured` (no `FACTORY_API_KEY` and CLI auth off) / `error` |

Overall status: `healthy` (all checks ok, 200), `degraded` (database ok, GitHub or Droid not ok, 200), `unhealthy` (database down, 503). `degraded` means the process is up but cannot start new agent work or reconcile GitHub state. The Docker healthcheck (see [Deployment](deployment.md)) fails only on `unhealthy`.

## Metrics and dashboard

`GET /metrics` returns `compute_metrics` from `app/metrics.py`: combined totals and `success_rate`, fix and review buckets with average and total runtime, `total_factory_credits`, cost fields (`agent_cost_usd`, `junior_cost_usd`, `productivity_gained_usd`, `cost_avoidance_percent`) together with the assumptions used, engineering KPIs (`merge_rate_percent`, `pr_cycle_time_hours`, `prs_last_7_days`, `change_failure_rate_percent`), and `daily` activity buckets for the last 14 days keyed to SGT calendar days. `recent_activity` in the same module backs the paginated history, and `GET /api/tasks` exposes it as JSON with `page` and `page_size` parameters (see [REST endpoints](api/rest-endpoints.md)).

`GET /dashboard` renders the same metrics plus the activity table for humans. Together, `/health` answers "is it up", the event stream answers "what is it doing", and `/metrics` answers "how well".
