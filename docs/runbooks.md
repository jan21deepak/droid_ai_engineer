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

Sessions persist on disk (`~/.factory`); startup recovery resumes orphaned fix rows
automatically (reviews are never resumed). If rows stay RUNNING for more than `DROID_TURN_TIMEOUT_SECONDS`
(default 3600):

1. Check the logs for `recovery.dispatch_lost` / `recovery.session_lost`.
2. Verify the droid CLI is authenticated on the host: `droid --version`.
3. Restart the service to re-trigger recovery.

### Completed fixes have no PR

This is expected. A successful fix records a local candidate (`candidate` on the
task API: `base_sha`, `sha`, `branch`, `status`) and never pushes or opens a PR on
its own. Look for `task.candidate_recorded` in the logs. A `candidate.status` of
`dirty` means the agent left uncommitted changes; `no_changes` means the agent
made no commit; `unavailable` means the workspace could not be inspected.

### Review failed with "review not started"

Forge refuses to review a partial change. The error names the reason: the PR's
base branch could not be fetched, the head was missing, the diff was empty, or the
diff was larger than the review limit. Interrupted reviews are also failed on
restart instead of resumed; reassign the PR to start a fresh restricted review.

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
