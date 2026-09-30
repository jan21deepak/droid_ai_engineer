# How to contribute

Droid Forge is a small codebase: 13 modules under `app/`, a 9-file test suite, a vanilla-JS dashboard, and one process that runs everything. Contributions that keep that shape are the ones that land. This page is the front door; the four pages below cover the details.

## Picking up work

Three sources of work:

- Open issues in this repository.
- The "Future Improvements" list in `README.md`. It names concrete directions: queue-based dispatch for horizontal scaling, a Postgres backend, a workspace retention policy, streaming session events into the dashboard, notifications, dashboard auth, per-repo prompt templates, and Droid Computers as remote compute targets.
- Gaps you find yourself. A missing test for an endpoint or worker path is almost always welcome, because the suite is the safety net for everything else.

For a first contribution, adding tests or a small endpoint behavior is a good size. The Droid refactor landed together with a rewritten suite, and the bugfix commit that followed it is a good study in what the suite catches and what only an end-to-end run catches.

## Before you write code

Read [Architecture](../overview/architecture.md) for the component map, then [Patterns and conventions](patterns-and-conventions.md) for the house rules. The short version:

- Settings live in the pydantic model in `app/config.py`; nothing reads `os.environ` directly.
- Logging goes through `log_event(logger, level, event_name, **ctx)` from `app/logging_conf.py`, and `event`, `logger`, and `level` are reserved keyword names you must never pass as context.
- Database access goes through the `db_session()` context manager from `app/database.py`.
- Slow work (clones, session launches, turns) runs in background asyncio tasks registered through the `DroidClient` run registry in `app/droid_client.py`.

## The PR process

1. Branch from the default branch. One change per branch, descriptive name.
2. Make the change and add or extend tests. `pytest -q` must pass; the suite is fully offline, so keep it that way.
3. Commit with a subject line that states what the change does, then a body that explains why, as bullets if there is more than one point. The existing history reads like this: imperative subject, detailed body, test count when it changed.
4. If an agent did the work, credit it with a `Co-authored-by` trailer. The history does this consistently.
5. Push and open a pull request against the default branch.

## Review expectations

There is no linter or formatter wired in, so review is by eye and the checks below are what reviewers look for:

- Route handlers stay fast and return quickly; no blocking work on the request path.
- New settings are added to `app/config.py`, `.env.example`, and the README variable table together.
- Log events are structured and named, and no context key collides with the reserved `log_event` parameter names.
- Database rows are reloaded or updated after cross-session mutations (the detached-snapshot trap that once produced "Pull Request: n/a" in completion comments).
- Tests stub collaborators at the module boundary rather than reaching the network.

## Definition of done

A change is done when all of these hold:

- `pytest -q` passes, with new tests covering the new behavior.
- New log events exist for operator-visible behavior changes.
- New or changed settings are documented in `.env.example` and `README.md`.
- Dashboard changes have been looked at in a browser (static assets are cache-busted by `asset_version` in `app/app.py`, so verify the version token changed).
- Anything touching the launch or review path was exercised end to end against a throwaway repo, not just unit tested. The four bugs fixed on 2026-09-28 were invisible to the suite and only showed up on a real fork.
- The commit message explains the reasoning.

## Next steps

- [Development workflow](development-workflow.md): branch, code, test, PR, merge, and how to point a local run at a sandbox repo.
- [Testing](testing.md): the fixtures, the stubs, and how to add tests.
- [Debugging](debugging.md): the runbook for when a run goes wrong.
- [Tooling](tooling.md): everything that builds, runs, or inspects the service.
