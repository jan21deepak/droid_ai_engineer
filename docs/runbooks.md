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

### Approval or publication refused (403/404/409)

Both requests name the full candidate SHA shown on the task page. For
`not_ready_for_approval` and `not_ready_to_publish`, the 409 response includes a
`blockers` array. The same details are available in
`GET /api/tasks/{id}/delivery` as `readiness.approval_blockers` and
`readiness.publish_blockers`.

| Response detail | Meaning | Operator next step |
|---|---|---|
| `stale_candidate` | The requested SHA is not the task's current candidate, the candidate changed during the request, or the workspace HEAD moved away from the recorded candidate. | Reload the task, then run checks and the restricted review again and use the current SHA for approval. |
| `dirty_workspace` | The task workspace has uncommitted changes. | Resolve the workspace changes, then retry; rerun checks and review if the candidate changed. |
| `workspace_unavailable` | Forge could not inspect the task workspace to verify the candidate. | Restore workspace/service availability and retry. |
| `candidate_not_ready` | There is no clean, committed candidate ready for delivery. | Wait for the fix to finish with a clean commit, or address a candidate with status `dirty`, `no_changes`, or `unavailable` before retrying. |
| `task_active` | An agent turn is still running or winding down for the task. | Wait for it to finish, or stop it, then retry. |
| `task_busy` | Another task change, or candidate finalization, is currently being admitted or recorded. | Wait for that operation to finish, then retry publication. |
| `publication_in_progress` | Publication is already running for this task or candidate. | Wait for publication to finish, then retry the refused request. |
| `not_ready_for_approval` | Required acceptance checks or the restricted candidate review are missing, incomplete, or not approving. | Follow the listed blockers: run or rerun checks and the restricted review, then approve the exact SHA. |
| `not_ready_to_publish` | The exact candidate is missing a prerequisite such as approval, checks, or review. | Follow the listed blockers, approve the exact SHA, then publish it. |
| `policy_denied` (403) | Outward-action policy does not allow the candidate branch push or draft PR, including an unlisted or default branch. | Use an allowed non-default branch or have the operator policy updated, then retry. |
| `repository_not_allowed` (403) | Repository policy no longer allows this repository for delivery. | Have the repository approved in Forge policy, then retry. |
| `task not found` (404) | The task ID does not exist. | Reload the task page and verify the task ID. |

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

### Repository reference rejected or secret scan flags a test fixture

**Accepted forms** for a GitHub repository reference:
- Slug: `owner/repo`
- HTTPS URL: `https://github.com/owner/repo` (or `http://`)
- Scheme-less: `github.com/owner/repo` or `www.github.com/owner/repo`
- SSH SCP: `git@github.com:owner/repo`
- SSH URL: `ssh://git@github.com/owner/repo`

Hosts are compared case-insensitively and must be exactly `github.com` or
`www.github.com`. One trailing `.git` is dropped for URL and SSH forms when a
repository name still remains. Owner and name may contain only ASCII letters,
digits, `_`, `.` or `-`.

**Rejected** inputs include: any other host (including visually similar ones),
credentials or passwords in the URL, ports, query strings, fragment identifiers,
`.` or `..` path components, and any characters outside the allowed set.

**`.gitleaksignore`** holds only exact `commit:path:rule:line` fingerprints for
synthetic test fixtures that are not real credentials. Any new real finding still
fails the scan. Do not add rule, path or value allowlists, CodeQL suppressions
or alert dismissals.

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
