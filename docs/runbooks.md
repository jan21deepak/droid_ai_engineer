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

### Tasks INTERRUPTED after a restart

Startup never resumes a Droid session. A fix row that was running with a saved
session becomes `interrupted` (log event `recovery.task_interrupted`); one that never
got a session fails with "dispatch interrupted". Orphaned reviews fail and are never
resumed. Each orphaned attempt keeps the usage it had observed.

1. Inspect the task: `GET /api/tasks/{id}` shows its attempts and usage coverage.
2. Verify the droid CLI is authenticated on the host: `droid --version`.
3. To continue the session, call `POST /api/tasks/{id}/recover`. It re-checks policy
   and appends a new `recovery` attempt; the interrupted attempt stays as it was.
   For a review, reassign the PR to start a fresh restricted review.

A follow-up or recovery that returns **409** means the task already has an active
attempt or its session is still streaming; wait for it to finish.

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

### Cost per accepted change says "unknown"

Expected until every attempt has a dollar value. Factory credits are observations;
Forge never converts them to dollars on its own. Record an explicit estimate
(`FORGE_USD_PER_CREDIT_ESTIMATE` with `FORGE_USD_ESTIMATE_SOURCE`) or a reconciled
amount on each attempt, and check `usd.attempts_unknown` in `GET /api/economics`.
A `no_accepted_changes` state means no candidate has been approved yet. The capacity
scenario stays empty until baseline assumptions are saved and each accepted change
has human review/rework minutes; those values are user-entered, not measured.

### Sharing evidence for one task

Use **Preview evidence** or **Download JSON** on the task page
(`GET /api/tasks/{id}/evidence?download=true`, operator sign-in required). The file
covers only that task and omits summaries, thinking, tool/check output and
credentials, but it does include repository names, SHAs and user-entered notes, so
review it before sending it anywhere. Forge itself uploads nothing.

## Escalation

- Dashboard: `GET /dashboard` (overview, task history, agent activity indicators, session ids).
- Metrics: `GET /metrics` for success-rate and runtime regressions.
- Logs are single-line structured SGT events; grep by `task_id=` or `session_id=`.
- For Factory-side problems (credits, model availability), check
  <https://docs.factory.ai> and your Factory org dashboard.
