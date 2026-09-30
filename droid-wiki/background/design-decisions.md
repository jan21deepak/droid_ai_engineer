# Design decisions

Droid Forge is small enough that its shape is a set of deliberate choices rather than an emergent architecture. This page records those choices and the trade-off each one accepts. Every claim points at the file that implements it.

## Local Droid sessions instead of a remote agent API

`app/droid_client.py` opens a `droid_sdk.Session` with `cwd` set to a workspace clone and streams the turn with `session.stream`. The `droid-sdk` package drives the `droid` CLI as a local subprocess, so the agent runs on the same machine as the FastAPI app, in the deployment's venv, laptop, or container.

Trade-off: the host must stay up for the whole run and needs `git` plus the `droid` CLI (both installed in `Dockerfile`). There is no queue and no remote sandbox to absorb a long run, and the service owns its own git plumbing, which is where one of the end-to-end bugs came from. In return there is no per-run remote environment to provision and no second control plane.

## Event-driven finalization plus a narrow reconciliation loop

`DroidClient._run_turn` awaits the stream and then calls the `on_complete` callback it was given. `app/worker.py` supplies that callback: `finalize_fix_task` and `finalize_review_task` own everything that happens after a turn (persisting the outcome, opening the PR, posting the comment and the review). The background `worker_loop` only reconciles GitHub-side state through `poll_github_state`, which refreshes PR open/closed/merged and completes tasks whose PR merged.

Trade-off: completion semantics depend on a callback that runs inside the same asyncio task that streamed the turn, so a crash between the turn ending and the callback's writes loses that work unless session resume replays it. Polling a remote status API is easier to reason about but adds latency and steady API traffic. Keeping the loop narrow means GitHub stays authoritative for merges without re-querying the agent.

## Per-task workspace clones

`prepare_fix_workspace` clones into `WORKSPACE_ROOT/<owner>/<repo>/task-<id>` and sets an authenticated `origin` (`https://x-access-token:<token>@github.com/...`) plus a Droid Forge git identity. Reviews use a sibling `review-<id>` directory from `prepare_review_workspace`.

Trade-off: every task and review allocates a full clone, so disk grows with run count and the README lists a retention policy as future work. Clones are not pooled or shared between tasks. In return each run is isolated, a restart can reuse the existing directory and resume the session in place, and the push remote is scoped to one task at a time.

## A read-only second session for review, not a separate reviewer product

`start_review_run` opens a second Droid session in a review workspace and `finalize_review_task` turns the agent's `VERDICT:` line into a GitHub PR review through `create_pull_request_review`. The review prompt tells the agent to inspect the diff, leave a verdict, and not modify files or push.

Trade-off: the read-only guarantee is enforced by the prompt and autonomy settings, not by a sandbox boundary, and the reviewer is the same model family that wrote the fix. Each review also costs another session. In return there is no second vendor, API key, or webhook to integrate, and both agent roles share one workspace and SDK vocabulary.

## Restart recovery via session resume

The `lifespan` handler in `app/app.py` calls `recover_interrupted_tasks` before starting the worker loop. `DroidClient.resume_interrupted` uses `Session.resume` plus a continuation prompt to nudge an orphaned session forward. Rows that never got a session id are marked failed as "dispatch interrupted by service restart".

Trade-off: recovery depends on session state surviving on disk. The app stores it under the droid CLI's home directory, which `docker-compose.yml` mounts as a named volume. If that state is lost, active rows are failed rather than retried. In return a deploy or crash does not throw away an hour of agent work.

## Per-repo model and setup command

`Repository.droid_model` and `Repository.setup_command` let one dashboard row differ from another, and `resolve_agent_launch_config` in `app/repos.py` resolves them for both fix and review launches. The setup command runs inside the fresh clone before the session opens.

Trade-off: configuration is attached to a registered repository, so an unregistered repo falls back to the global `DROID_MODEL` and a bare clone with no dependency install, which can leave the agent unable to run tests. In return one service can host repos with different toolchains (`npm ci`, `pip install -e .`) and different model choices without a separate deployment.

## Duplicate-task protection keyed on repository, issue, and status

`create_and_dispatch_task` in `app/tasks.py` rejects a new task when one already exists for the same repository and issue number in `QUEUED`, `RUNNING`, or `COMPLETED`, returning `TaskCreateError` with the existing task id. `ensure_review_task_row` in `app/worker.py` creates a review only when no review for that repo and PR is in `TaskStatus.ACTIVE`. The dashboard Assign flow leans on this guard because it both labels the issue (which can fire the webhook) and launches directly.

Trade-off: a completed fix task blocks re-dispatch, so an issue cannot be re-run through the same key, and a failed task can be retried only because `FAILED` is outside the guard set. For reviews the key is repo plus PR number within active status, so a commit pushed while a review runs is not re-reviewed. In return double-labeling an issue, or the dashboard and webhook firing together, does not start two agents on the same work.

## Related pages

- [Migration from Cursor](migration-from-cursor.md) for the history behind these choices.
- [Droid client](../systems/droid-client.md) for the session and workspace mechanics.
- [Worker](../systems/worker.md) for finalization and recovery in detail.
- [Data models](../reference/data-models.md) for the columns named here.
