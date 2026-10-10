# Alerting rules

The system notifies on-call when health or throughput degrade, rather than
waiting for someone to notice. Two layers:

## 1. Automated issue opener (`health-monitor.yml`)

A scheduled GitHub Actions workflow (every 15 minutes) hits the deployed
service's `GET /health` endpoint and **opens a GitHub issue when the service is
unhealthy** (`503`) or unreachable. Failed checks deduplicate: if an open issue
already exists for the current outage, it is updated instead of duplicated.

| Alert | Condition | Severity | Response |
|---|---|---|---|
| Service unreachable | `/health` times out or connection refused from the monitor | P1 | Check host/container is up (`docker compose ps`); check tunnel process |
| Service unhealthy | `/health` returns 503 | P1 | Database unreachable — follow [runbook](runbooks.md#health-triage) |
| Service degraded > 30 min | `/health` returns 200 but `checks.github` or `checks.droid` is not `ok` across 2+ consecutive checks | P2 | Token expired/unreachable external — fix `.env`, restart |

## 2. Manual dashboards

- **KPIs / throughput**: `GET /dashboard` — success rate, PR cycle time, daily activity.
- **Raw counters**: `GET /metrics` — totals, runtimes, cost.
- Alert when the fix success rate drops > 20 points below its 14-day average,
  or when average runtime doubles week-over-week (both visible in `/metrics`).

## Adding a new alert

1. Detect it in the monitor workflow (`.github/workflows/health-monitor.yml`)
   if it is checkable from outside, or via a log-grep script if internal.
2. Document the condition and response in the table above.
3. Link the runbook section that resolves it.
