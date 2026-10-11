# Incident response runbook

Operational procedures for running Droid Forge. Read before on-call work.

## Health triage

Start every incident with `GET /health` (public, database only), then the
operator-only `GET /api/health` (bearer or signed-in session), which reports
`database`, `github`, and `droid` independently:

| Status | Meaning | First action |
|---|---|---|
| `200 healthy` | All checks pass | No action |
| `200 degraded` | GitHub or Droid unreachable/unconfigured; app still serves | Fix tokens in `.env`, restart; check `status.factory.ai` and GitHub status |
| `503 unhealthy` | Database unreachable | Check `DATABASE_URL` path and disk space; SQLite file must be writable |

## Common incidents

### Webhook returns 401 (HMAC mismatch)

1. `GITHUB_WEBHOOK_SECRET` in `.env` must exactly match the secret configured on
   the repo's webhook. Rotating one side without the other causes this.
2. Update the secret on the webhook in the repository's GitHub settings. Forge
   never installs or edits webhooks itself (`POST /api/webhooks/sync` is refused
   by policy).

### Tasks stuck in RUNNING after a restart

Sessions persist on disk (`~/.factory`); startup recovery resumes orphaned rows
automatically. If rows stay RUNNING for more than `DROID_TURN_TIMEOUT_SECONDS`
(default 3600):

1. Check the logs for `recovery.dispatch_lost` / `recovery.session_lost`.
2. Verify the droid CLI is authenticated on the host: `droid --version`.
3. Restart the service to re-trigger recovery.

### PRs not being created despite completed fixes

1. Check the task row's `error` on the dashboard.
2. Look for `policy.outward_denied` in logs. Publishing needs
   `FORGE_ALLOW_GITHUB_WRITES=true`, the repository in `FORGE_ALLOWED_REPOSITORIES`
   and the agent's branch in `FORGE_ALLOWED_BRANCHES`; otherwise the commit stays
   local by design.
3. Verify `GITHUB_TOKEN` can open PRs on the target repo (same-repo PRs need
   `contents: write` + `pull requests: write`).
4. Look for `droid.branch_push_failed` / `polling.pr_fallback_error` in logs:
   forge's own push failed, or the branch was pushed but the PR could not be opened.

### Review posts fail with 403 on GitHub

GitHub rejects APPROVE/REQUEST_CHANGES from the token owner's own PRs. Forge
falls back to COMMENT automatically; if the fallback also fails, check
`github.review_post_failed` in the logs and the token's scopes.

## Escalation

- Dashboard: `GET /dashboard` (task history, KPIs, session ids).
- Metrics: `GET /metrics` for success-rate and runtime regressions.
- Logs are single-line structured SGT events; grep by `task_id=` or `session_id=`.
- For Factory-side problems (credits, model availability), check
  <https://docs.factory.ai> and your Factory org dashboard.
