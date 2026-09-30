# Patterns and conventions

## Configuration

Settings are a single pydantic-settings model in `app/config.py`, cached with `lru_cache`. Read them through `get_settings()` and never mutate a settings object in place. Tests clear the cache to pick up environment changes. Adding a setting means adding a field here, an entry in `.env.example`, and a row in the README table.

## Structured logging

Logging goes through `log_event(logger, level, event_name, **ctx)` in `app/logging_conf.py`. The third positional argument is the event name; every other detail is a keyword field. Both route through one formatter that appends the context dict.

One rule matters enough to repeat: **do not pass a keyword named `event`, `logger`, or `level`.** Those collide with the function's own parameters and raise `TypeError` at call time. Use a more specific name such as `review_event`. A bug of exactly this shape shipped once and silently broke review accounting; see [Migration from Cursor](../background/migration-from-cursor.md).

## Async boundaries

Route handlers stay fast. Anything that clones a repository, opens a session, or waits on a turn runs in a background asyncio task created with `asyncio.create_task` and registered through `DroidClient.track`. Webhook handlers return `202` immediately because GitHub abandons deliveries after about ten seconds.

Completion is delivered by callback, not polling. `DroidClient` accepts an `on_complete` callable and invokes it after the turn ends, before the session closes.

The worker loop is reserved for state that no callback can observe, mainly GitHub PR and merge state.

## Database access

Use the `db_session()` context manager from `app/database.py`. It commits on success, rolls back on exception, and closes the session. Rows are loaded, mutated, and flushed inside the block. Long work does not happen inside a session: load what you need, close, do the work, then open a new session to persist.

Detached snapshots are a known hazard. When a handler creates a related record after loading a row (for example opening a PR after loading a Task), update the in-memory object before rendering it, or reload. The completion comment once showed "Pull Request: n/a" for exactly this reason.

## Error handling

Failures that should mark a task failed are recorded on the row and logged, not raised past the callback. `finalize_fix_task` and `finalize_review_task` catch their own GitHub errors and log `github.*` events rather than letting an exception escape into the asyncio task.

`DroidRunError` signals an unusable Droid run (clone failed, session could not open). `DroidSessionBusyError` signals a turn already in flight. Both live in `app/droid_client.py`.

## Prompts and parsing

Agent instructions are templates in `app/droid_client.py` (`FIX_PROMPT_TEMPLATE`, `REVIEW_PROMPT_TEMPLATE`, `FOLLOW_UP_PROMPT_TEMPLATE`, `RECOVERY_PROMPT_TEMPLATE`). Build them through the `build_*` helpers so formatting stays in one place.

Every template requires a machine-readable final line: `BRANCH: <name>` for fixes, `VERDICT: APPROVE|REQUEST_CHANGES` for reviews. Parse results with `extract_branch_name` and `extract_verdict`; never infer the branch from git when the agent already reported it.

## Tests

Tests live in `tests/` and run with `pytest` (asyncio mode is `auto` via `pytest.ini`). Each test gets a fresh SQLite database from the autouse `clean_db` fixture in `tests/conftest.py`, which also forces Droid to an unconfigured state so no test reaches a real runtime. Stub collaborators at the module boundary: `StubGitHub` and `StubDroid` in `tests/test_worker.py` are the models to follow. HTTP-level tests use `respx`.

## Formatting and style

Standard Python. No linter is wired into the repo. Keep modules focused: one module per concern, matching the existing `app/` layout. Public functions carry a short docstring that says what they do and any contract they impose on callers.

## Related pages

- [Testing](../how-to-contribute/testing.md) for how to run and extend the suite.
- [Debugging](../how-to-contribute/debugging.md) for the runbook.
- [Configuration](../reference/configuration.md) for the settings reference.
