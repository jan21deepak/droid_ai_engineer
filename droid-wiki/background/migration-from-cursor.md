# Migration from Cursor

Droid Forge is version 2.0.0 of a project that first shipped as Cursor Forge, and before that as a Devin integration. This page records what the Cursor version did, how the refactor mapped its APIs onto Droid, and which bugs the first real run exposed.

## Timeline

The repository was seeded on 2026-08-09 with a Devin codebase as a baseline (`e4b4f1c`), then immediately rebranded: `299350c` replaced the Devin API with Cursor Cloud Agents and renamed the project Cursor Forge on the same day. The next day, `d434ebe` swapped the hand-rolled Cursor REST client for the official `cursor-sdk`. Several follow-ups that day and the next added per-repo Cursor environments, fork-safe PRs, and a Bugbot-only review flow (`eba9f5e`, `058383d`, `b9b2b7e`).

The Droid refactor landed on 2026-09-28 in `cb588ca` ("Refactor all Cursor APIs to Droid (Factory) APIs", 17:38 +0800), changing 33 files with 2681 insertions and 2874 deletions. A follow-up the same evening, `9b9dbed` ("Fix four bugs found by end-to-end validation on a real fork", 19:21 +0800), fixed defects that only appeared against a large repository.

## What Cursor Forge was

Cursor Forge delegated implementation to remote Cursor Cloud Agents. `app/cursor_client.py` wrapped the synchronous `cursor-sdk` and ran its calls through `asyncio.to_thread`. A task created a remote agent (`Agent.create`) with a model, an optional named Cloud Environment, and a repository, then called `agent.send(prompt)` to start a run. State lived in `cursor_agent_id` and `cursor_run_id`, and the worker polled status with `Agent.get_run` / `Agent.list_runs`. Reviews were not a forge agent: the worker commented `bugbot run` on the PR for Cursor Bugbot, and tracked that externally.

The workspace was remote too: a repo could name a Cursor Cloud Environment (`Repository.cursor_environment`), described by `.cursor/environment.json` (`458d539`).

## API mapping

| Cursor era | Droid era | Where |
| --- | --- | --- |
| `Agent.create` plus `agent.send(prompt)` | `Session(...)` opened with `session.open()`, turn driven by `session.stream(prompt)` | `app/droid_client.py` |
| `Agent.get_run` / `Agent.list_runs` status polling | `on_complete` callback fired from the streamed turn (`finalize_fix_task`, `finalize_review_task`) | `app/droid_client.py`, `app/worker.py` |
| `Agent.resume` for follow-ups | `Session.resume` for follow-ups and restart recovery | `app/droid_client.py` |
| `Cursor.models.list` for the model catalog | `list_models()` | `app/droid_client.py` |
| Bugbot review trigger (`bugbot run` comment) | second read-only Droid review session, then `create_pull_request_review` | `app/worker.py`, `app/github.py` |
| `cursor_environment` (named Cloud Environment) | per-repo `droid_model` plus `setup_command` | `app/models.py`, `app/repos.py` |
| `cursor_agent_id` / `cursor_run_id` | `droid_session_id` | `app/models.py` |
| `Cursor.me` connectivity check | `list_models()` connectivity check | `app/droid_client.py` |
| `close_default_client()` on shutdown | `DroidClient.shutdown()` cancels in-flight asyncio tasks | `app/droid_client.py` |
| Trigger label `Cursor-complete` | `Droid-complete` | `app/config.py` |
| `CURSOR_API_KEY`, `CURSOR_MODEL` (default `composer-2.5`) | `FACTORY_API_KEY`, `DROID_MODEL` (default `auto`), `DROID_ALLOW_CLI_AUTH` | `app/config.py` |
| `/api/cursor/environments` discovery endpoint | `/api/droid/models` model catalog | `app/app.py` |

## What was deleted

The refactor removed the Cursor runtime outright: `app/cursor_client.py` (595 lines), `.cursor/environment.json`, `tests/test_cursor_client.py` (366 lines), and `tests/test_environments.py` (38 lines). `app/droid_client.py` (702 lines) replaced the client. `requirements.txt` swapped `cursor-sdk==1.0.26` for `droid-sdk>=0.4.0` and moved the remaining pins from `==` to `>=`. The Bugbot trigger was dropped from `app/github.py` and no `bugbot` reference remains anywhere in `app/`. The app title and version moved from `cursor-ai-engineer` / `1.0.0` to `droid-ai-engineer` / `2.0.0` in `app/config.py` and `app/__init__.py`.

## Schema rename and the no-migration caveat

The rename reached the database. `Task` and `ReviewTask` both lost `cursor_agent_id` and `cursor_run_id` and gained `droid_session_id`; both gained `factory_credits`, and `Task` gained `estimated_tokens`. `Repository.cursor_environment` became `setup_command` plus `droid_model`.

Existing v1 SQLite files are not migrated. `_run_lightweight_migrations` in `app/database.py` adds columns that exist in the models but not in the table, so a v1 database will gain the new columns, but the old `cursor_*` columns stay behind and hold the only copy of the v1 identifiers. The README states the consequence directly: point `DATABASE_URL` at a fresh database. There is no backfill, because a Cursor agent id and a Droid session id name different systems.

## The four bugs from the end-to-end run

`9b9dbed` fixed four defects that only showed up when the full flow (issue, Droid fix, PR, Droid review) ran against a fork with roughly a thousand files.

1. **Review sessions ran at `Autonomy.OFF`.** Headless turns auto-reject permission requests, so the review agent's first read command was rejected and the run aborted with `permission_rejected`. The fix added `DROID_REVIEW_AUTONOMY` (default `high`) and stopped forcing `OFF` for reviews.
2. **The review workspace had no merge base.** The workspace was a `--depth 1` clone, so `git diff <base>...HEAD` failed with "no merge base". The fix clones blobless with full history, checks out the PR head, then materializes the base branch after checkout so git does not refuse to update the current worktree ref (`prepare_review_workspace` in `app/droid_client.py`).
3. **The completion comment said "Pull Request: n/a".** The detached task snapshot was not updated after the PR was created in the same handler. `finalize_fix_task` now assigns the new URL onto the snapshot before rendering the comment.
4. **Review posting reported `posted=False` after GitHub accepted it.** `create_pull_request_review` logged with an `event=` context key, colliding with `log_event`'s own `event` parameter. The `TypeError` fired after GitHub accepted the review, so success accounting was wrong. The keyword was renamed to `review_event`.

The commit added 85 passing tests, including coverage for review autonomy, the merge-base clone, the completion comment, and review posting.

## Related pages

- [Design decisions](design-decisions.md) for the reasoning behind the Droid shape.
- [Glossary](../overview/glossary.md) for terms such as session, turn, and verdict.
- [Data models](../reference/data-models.md) for the current column list.
- [Configuration](../reference/configuration.md) for the current environment variables.
