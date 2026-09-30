# Data models

Persistence is SQLAlchemy 2.x mapped columns over SQLite, declared in `app/models.py`. A single `Base(DeclarativeBase)` holds five models: `Repository`, `SyncedIssue`, `Task`, `SyncedPullRequest`, and `ReviewTask`. All timestamps are stored timezone-aware in UTC and rendered for the UI in Singapore time (SGT) by `iso_sgt` and `format_sgt` from `app/timeutil.py`.

The models have no ORM `relationship()` declarations. They relate by string value: a task or review carries the repository `full_name`, and joins happen on that string plus the issue or PR number. That keeps the schema flat and lets a task outlive the deletion of a `Repository` row.

## TaskStatus

`TaskStatus` in `app/models.py` is a plain class of string constants, not an enum column. Status columns store the string.

| Constant | Value | Meaning |
| --- | --- | --- |
| `QUEUED` | `queued` | Row written, Droid session not yet open |
| `RUNNING` | `running` | Session open and a turn streaming |
| `COMPLETED` | `completed` | Turn succeeded, or the PR merged |
| `FAILED` | `failed` | Turn failed, or the launch was interrupted |

Two tuples drive queries: `ACTIVE = (QUEUED, RUNNING)` is used by recovery and duplicate checks for reviews, and `ALL` is the full set. Fix-task duplicate protection in `app/tasks.py` uses an explicit `(QUEUED, RUNNING, COMPLETED)` set instead, so a failed fix can be retried.

## Repository

Registered GitHub repositories. One row per repo, unique on `full_name`.

Key columns: `full_name` (unique, indexed), `url`, `description`, `setup_command`, `droid_model`, `starting_ref`, `created_at`.

`setup_command` is an optional shell command run in the fresh clone before a session starts (`pip install -e .`, `npm ci`). `droid_model` is an optional per-repo model override; empty means use `DROID_MODEL`. `starting_ref` is the ref fix workspaces clone and the base branch for PRs.

`to_dict` returns `id`, `full_name`, `url`, `description`, `setup_command`, `droid_model`, `starting_ref`, `created_at`, `created_at_display`.

## SyncedIssue

Open GitHub issues imported for manual assignment from the dashboard. Unique on `(repository, issue_number)` (`uq_repo_issue`).

Key columns: `repository` (indexed), `repository_url`, `issue_number` (indexed), `title`, `body`, `state`, `labels` (a JSON string), `html_url`, `synced_at`.

`to_dict` returns `id`, `repository`, `repository_url`, `issue_number`, `title`, `body`, `state`, `labels`, `html_url`, `synced_at`, `synced_at_display`.

## Task

One issue-fix run: a Task row plus a Droid fix session in a `task-<id>` workspace.

Key columns: `repository` (indexed), `repository_url`, `issue_number` (indexed), `issue_title`, `issue_body`, `labels`, `droid_session_id` (indexed, nullable), `status` (indexed), `pull_request_url`, `pr_state`, `pr_merged_at`, `summary`, `error`, `factory_credits`, `estimated_tokens`, `cost_usd`, `created_at`, `completed_at`, `duration_seconds`.

`droid_session_id` is written when the launch succeeds and is what restart recovery resumes. `pr_state` is one of `open`, `closed`, or `merged`, refreshed from GitHub by the worker loop so delivery metrics do not depend on a `ReviewTask` row existing.

`to_dict` returns `id`, `repository`, `repository_url`, `issue_number`, `issue_title`, `droid_session_id`, `droid_session_url` (always `None`), `status`, `pull_request_url`, `pr_state`, `merged` (true when `pr_state == "merged"`), `pr_merged_at`, `summary`, `factory_credits`, `estimated_tokens`, `cost_usd`, `created_at`, `created_at_display`, `completed_at`, `completed_at_display`, `duration_seconds`, and `kind` (`"fix"`). The `issue_body` and `labels` columns are not exposed.

## SyncedPullRequest

Open, non-draft pull requests available for review assignment. Unique on `(repository, pr_number)` (`uq_repo_pr`).

Key columns: `repository` (indexed), `repository_url`, `pr_number` (indexed), `title`, `body`, `html_url`, `head_sha`, `author`, `draft`, `synced_at`.

`to_dict` returns `id`, `repository`, `repository_url`, `pr_number`, `title`, `body`, `html_url`, `head_sha`, `author`, `draft`, `synced_at`, `synced_at_display`.

## ReviewTask

One review run against a pull request, including auto-merge bookkeeping.

Key columns: `repository` (indexed), `repository_url`, `pr_number` (indexed), `pr_title`, `pr_url`, `commit_sha`, `droid_session_id` (indexed, nullable), `status` (indexed), `merged`, `auto_merge_enabled`, `summary`, `error`, `factory_credits`, `cost_usd`, `created_at`, `completed_at`, `duration_seconds`.

`auto_merge_enabled` records that GitHub auto-merge was turned on because an immediate merge was blocked. There is no unique constraint; `ensure_review_task_row` in `app/worker.py` enforces one active review per repo and PR in application code.

`to_dict` returns `id`, `repository`, `repository_url`, `pr_number`, `pr_title`, `pr_url`, `commit_sha`, `droid_session_id`, `droid_session_url` (always `None`), `status`, `merged`, `auto_merge_enabled`, `summary`, `error`, `factory_credits`, `cost_usd`, `created_at`, `created_at_display`, `completed_at`, `completed_at_display`, `duration_seconds`, and `kind` (`"review"`).

## Cursor-era renames

The v2.0 refactor changed several columns in place. `Task.cursor_agent_id` and `Task.cursor_run_id` collapsed into `Task.droid_session_id`; `ReviewTask` got the same treatment. `Repository.cursor_environment` (a named Cursor Cloud Environment) became `setup_command` plus `droid_model`. `Task` gained `factory_credits` and `estimated_tokens`, and `ReviewTask` gained `factory_credits`. The `to_dict` output dropped `cursor_agent_id`, `cursor_run_id`, and `cursor_agent_url`, and added `factory_credits`, `estimated_tokens`, and `droid_session_url`.

`_run_lightweight_migrations` in `app/database.py` inspects existing tables and adds any column present in the models but missing from the table with a plain `ALTER TABLE ... ADD COLUMN`, logging `db.migration_applied`. It adds new columns only; there is no rename or backfill, which is why a v1 SQLite file should be replaced rather than reused.

## Related pages

- [Persistence](../systems/persistence.md) for the engine, sessions, and migration runner.
- [Configuration](configuration.md) for the settings that shape these rows.
- [REST endpoints](../api/rest-endpoints.md) for where the `to_dict` shapes are returned.
- [Migration from Cursor](../background/migration-from-cursor.md) for the schema rename in context.
