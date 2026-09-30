# Debugging

A runbook for failed or stuck tasks. Work through it in order: logs, database, workspace clone, session transcript, then GitHub itself.

## Read the structured logs

Every log line comes from `log_event` in `app/logging_conf.py`, formatted as `timestamp | LEVEL | logger | event_name | key=value ...`, with ISO-8601 timestamps in Singapore time. Example from a healthy fix turn:

```
2026-09-28T11:30:00.123+08:00 | INFO | app.droid | droid.turn_finished | session_id=sess-… kind=fix success=True events=142 duration_seconds=734.0
```

The event names to know:

| Stage | Events |
| --- | --- |
| Webhook | `webhook.received`, `webhook.ignored`, `webhook.signature_invalid`, `webhook.signature_skipped` |
| Task | `task.created`, `task.status_changed`, `task.completed`, `task.failed`, `task.follow_up` |
| Droid session | `droid.agent_linked`, `droid.session_started`, `droid.turn_progress` (DEBUG only), `droid.turn_finished`, `droid.not_configured`, `droid.session_start_failed` |
| PR | `github.pr_created`, `github.pr_reused`, `github.pr_fallback_failed`, `github.pr_create_rejected`, `pr.state_changed` |
| Review | `droid.review_started`, `droid.review_tracked`, `review.status_changed`, `review.completed`, `review.failed`, `github.review_posted`, `github.review_post_failed` |
| Restart | `recovery.dispatch_lost`, `recovery.session_lost`, `recovery.completed` |
| Loop and DB | `worker.started`, `worker.iteration_error`, `db.initialized`, `db.migration_applied` |

`droid.turn_finished` carries `success` and `subtype`; that pair is the first thing to read on any failure. `droid.turn_progress` only appears at `LOG_LEVEL=DEBUG`, so set that when a session looks quiet.

## Inspect the SQLite database

`DATABASE_URL` defaults to `sqlite:///./data/tasks.db` (`/data/tasks.db` in Docker). Tables map one-to-one to the models in `app/models.py`:

```bash
sqlite3 data/tasks.db "select id, repository, issue_number, status, droid_session_id, pull_request_url, error from tasks order by id desc limit 5;"
sqlite3 data/tasks.db "select id, pr_number, status, merged, droid_session_id, error from review_tasks order by id desc limit 5;"
sqlite3 data/tasks.db "select id, full_name, droid_model, setup_command, starting_ref from repositories;"
```

Status values are `queued`, `running`, `completed`, `failed`. `error` holds the failure reason, `droid_session_id` links the row to its transcript, and `pull_request_url` plus `pr_state` track delivery. The Docker image ships no `sqlite3` binary, so inside the container query through Python's `sqlite3` module against `/data/tasks.db`.

## Find the workspace clone

Workspaces live under `WORKSPACE_ROOT` (default `./data/workspace`), at `<root>/<owner>/<repo>/task-<id>` for fixes and `<root>/<owner>/<repo>/review-<id>` for reviews (`task_workspace_dir` and `review_workspace_dir` in `app/droid_client.py`).

```bash
cd data/workspace/owner/repo/task-7
git status
git branch --show-current
git log --oneline -5
```

The clone's `origin` embeds the GitHub token (`https://x-access-token:...@github.com/...`), so never paste `git remote -v` output anywhere. To check the remote safely: `git remote get-url origin | sed 's#//[^@]*@#//#'`.

If the workspace has no `.git`, the initial clone failed; the error is in the task row and the `droid.session_start_failed` log event. Fix workspaces are shallow clones; review workspaces are blobless full-history clones so the three-dot diff has a merge base.

## Read the Droid session transcript

The droid CLI stores sessions under its home directory, `~/.factory` locally and `/home/appuser/.factory` in Docker (mounted as the `droid_home` volume, so sessions survive restarts).

```bash
sqlite3 data/tasks.db "select droid_session_id from tasks where id=7;"   # get the id
jq '.entries[] | select(.sessionId=="<session-id>")' ~/.factory/sessions-index.json
find ~/.factory/sessions -name "<session-id>.jsonl"
tail -f ~/.factory/sessions/<workspace-dir>/<session-id>.jsonl
```

The directory name is the workspace path with slashes replaced by dashes, and the transcript is one JSON object per line, starting with a `session_start` record. The index file gives the title, cwd, and message count without opening the transcript. The same paths work in Docker, under `/home/appuser/.factory`.

## Common failures and their signatures

| Symptom | Signature | Cause and fix |
| --- | --- | --- |
| Task never starts, stays `queued` | `droid.not_configured` in logs, `/health` shows `droid: unconfigured` | No `FACTORY_API_KEY` and `DROID_ALLOW_CLI_AUTH` is false. Set the key (always in Docker) or enable CLI auth locally. |
| Webhook rejected | `webhook.signature_invalid`, HTTP 401 | `GITHUB_WEBHOOK_SECRET` does not match the secret configured on the GitHub webhook. Fix `.env` and re-run `POST /api/webhooks/sync`. |
| Review aborts immediately | `droid.turn_finished` with `subtype=permission_rejected`, task error "Droid run ended with status: permission_rejected" | Session autonomy too low. Headless runs auto-reject permission prompts. Keep `DROID_REVIEW_AUTONOMY` at `high`; never set it to `off`. |
| Review diff fails | "no merge base" in the review output, `droid.review_base_ref_failed` in logs | Base branch missing in the review clone, usually a wrong `starting_ref` or renamed default branch. Check the repository row. |
| Second trigger does nothing | Webhook returns 200 with "duplicate: task already exists" | A `queued`/`running`/`completed` task for that repo and issue already exists. Not an error; reassigning is blocked by design. |
| PR not opened | `github.pr_fallback_failed` or `github.pr_create_rejected` | GitHub rejected the PR (bad base ref, missing token scope, or a PR already exists for the branch). The log context carries the branch, base, and error. |
| Review verdict posted as plain comment | `github.review_post_failed` then a COMMENT fallback | GitHub rejects APPROVE/REQUEST_CHANGES on the token owner's own PR. Expected fallback; use a different token owner for verdict reviews. |
| Follow-up rejected | HTTP 409 "the Droid session is still running a turn" | A turn is streaming. Wait for `droid.turn_finished` and retry. |
| Tasks failed after a restart | `recovery.dispatch_lost` (no session yet) or `recovery.session_lost` (session unresumable) | Restart interrupted dispatch or the session file is gone. The rows are marked failed; re-trigger the issue. |
| GitHub calls fail with 403 | `github.*` events with 403 details | The PAT lacks the needed permissions (issues, labels, PRs, reviews). Check the token scopes. |

## Check GitHub state with `gh`

```bash
gh issue view 12 --repo owner/repo
gh pr list --repo owner/repo
gh pr view 34 --repo owner/repo --json state,mergedAt,headRefName,baseRefName
gh api repos/owner/repo/hooks                                  # webhook configs
gh api repos/owner/repo/hooks/<id>/deliveries                  # delivery log with response codes
gh api repos/owner/repo/issues/12/comments
```

Compare what GitHub reports against `pr_state` in the `tasks` table; the worker loop reconciles them every `POLL_INTERVAL_SECONDS` (default 20), so the database can lag by one interval.

## Related pages

- [Architecture](../overview/architecture.md) for which component owns which step.
- [Testing](testing.md) for reproducing a failure in the offline suite.
- [Tooling](tooling.md) for the database and CLI tooling used above.
