# Lore

Droid Forge has had three names in seven weeks. The history is short (15 commits, 2026-08-09 to 2026-09-28) but dense, and every event below is anchored to a real commit. Dates and times are the author-local (+08:00) values from `git log`. Where a motive is not stated in the history, the text says so.

## The Devin baseline (August 9, 2026)

The repository starts at 17:50 with a single one-line README (e554f05). At 21:20 the same evening, commit e4b4f1c seeds the entire "Devin Forge" codebase as a "migration baseline": 34 files including `app/devin.py` (318 lines), the worker, the dashboard, the metrics module, and a test suite. Even at this point the service already has its lasting shape: webhook intake, SQLite task rows, a dashboard, a metrics endpoint, and a review stage.

Twenty-three minutes later (299350c, 21:43), the Devin API is gone, replaced with Cursor Cloud Agents, and the project is rebranded Cursor Forge. `app/devin.py` is deleted, `app/cursor_client.py` (337 lines) takes its place, and the trigger label becomes `Cursor-complete`. The Devin era survives only in git history.

## The Cursor Forge era (August 10 to 11, 2026)

August 10 is the busiest day in the history, with eight commits, one of them the merge of pull request #1.

- 13:30 (3e0fb51) and 14:18 (1417f6e): the remaining Devin references come out of the README, and the stale architecture PNG is replaced with a Cursor Forge diagram.
- 18:00 (d434ebe): the hand-rolled Cursor REST client is replaced with the official `cursor-sdk` end to end. `app/cursor_client.py` eventually grows to 595 lines.
- 19:19 (458d539): a commit authored by "Cursor Agent" adds `.cursor/environment.json`, a Cloud Agent dev environment (an install command, one terminal running uvicorn on port 8000). It arrives through pull request #1, merged at 20:13 (7edd988). The service's own dev environment was contributed by the same class of remote agent it managed. That looks like deliberate dogfooding, though no commit says so outright.
- 23:42 (eba9f5e): the biggest Cursor-era feature commit. Per-repo `cursor_environment` launches agents in a named environment, PRs become fork-safe (same-repo PRs opened with the PAT), review switches to a Bugbot-only flow (forge comments `bugbot run` on each PR so Cursor Bugbot picks it up), and auto-merge stays off.
- 23:45 (4a3af64): "Refresh remaining files so GitHub no longer attributes them to migration commits" adds one line to 15 files. Presumably this cleared a GitHub attribution the maintainer did not want, but the commit does not explain further.
- 23:46 (604a15b): a fintech/fineract positioning line comes out of the README.

August 11 has two hardening commits:

- 13:24 (058383d): the webhook assign flow is hardened, same-repo PR URLs are persisted before the Bugbot handoff, completed tasks missing PR links are recovered, and `PUBLIC_BASE_URL` label/webhook automation lands.
- 13:38 (b9b2b7e): Bugbot review runtimes are tracked for the dashboard Review Time Stats, creating `ReviewTask` rows and measuring duration from the `bugbot run` comment to the `cursor[bot]` BUGBOT_REVIEW reply.

Five of these commits carry `Co-authored-by: Cursor`. The app name is `cursor-ai-engineer`, the label is `Cursor-complete`, and code review is delegated to Cursor's Bugbot. Nearly every commit in the whole history is authored by the maintainer `jan21deepak`, with agent identities appearing as authors or co-authors.

## Quiet weeks (August 11 to September 28, 2026)

Seven weeks pass with no commits. What happened during that stretch is not recorded in the repository; the next event is the rewrite.

## The Droid refactor (September 28, 2026)

At 17:38, cb588ca "Refactor all Cursor APIs to Droid (Factory) APIs" replaces the remote-agent runtime with local Droid sessions driven by the official `droid-sdk`. It is the largest commit in the history: 33 files, 2,681 insertions, 2,874 deletions.

- `app/cursor_client.py` (595 lines) is deleted; `app/droid_client.py` (702 lines) replaces it.
- `app/worker.py` is rewritten (1,479 changed lines). Completion becomes event-driven: finalize callbacks replace the remote status polling.
- The schema renames `cursor_agent_id` to `droid_session_id`, and per-repo `cursor_environment` becomes per-repo `droid_model` plus a `setup_command`.
- The Bugbot trigger is dropped; a second Droid review session posts GitHub PR reviews directly.
- `.cursor/environment.json`, the `/api/cursor/environments` endpoint, and their tests are removed. `/api/droid/models` is added.
- Docker now installs git and the droid CLI and persists `~/.factory` sessions.
- The test suite is rewritten and stands at 80 passing tests.

Both September commits carry `Co-authored-by: factory-droid[bot]`, so the migration off Cursor agents appears to have been executed by a Droid agent itself.

## End-to-end hardening (September 28, 2026)

About 100 minutes later, at 19:21, commit 9b9dbed fixes four bugs found by running the full flow (issue, fix, PR, review) against a real fork of roughly a thousand files:

1. Review sessions ran at `Autonomy.OFF`. Headless turns auto-reject permission requests, so the review agent's first read command was rejected and the run aborted with `permission_rejected`. The fix adds `DROID_REVIEW_AUTONOMY` (default `high`).
2. Review workspaces were shallow clones, so `git diff <base>...HEAD` failed with "no merge base". The fix clones blobless full-history, fetches the PR head, then materializes the base branch after checkout.
3. Completion comments said "Pull Request: n/a" because the detached task snapshot was not updated after the PR was created in the same handler.
4. `create_pull_request_review` logged with an `event=` context key that collided with `log_event`'s own `event` parameter. The `TypeError` fired after GitHub accepted the review, so success accounting reported `posted=False`.

The suite grows to 85 tests with the new coverage. See [Migration from Cursor](background/migration-from-cursor.md) for the technical side of the rewrite.

## Longest-standing features

- Present since the August 9 seed: HMAC-verified webhook intake, SQLite persistence through SQLAlchemy, the dashboard and metrics endpoints, and the 202-fast webhook response.
- Since August 10 (eba9f5e): fork-safe same-repo pull requests opened with the PAT.
- The follow-up API (`POST /api/tasks/{task_id}/follow-up`) dates to the Cursor era and carried over as `Session.resume`.
- The worker loop also predates the refactor. In the Cursor era it polled a remote API for agent status; since the refactor it only reconciles GitHub-side PR and merge state.

## Deprecated and removed

| Feature | Era | Removed |
| --- | --- | --- |
| Devin client (`app/devin.py`, `tests/test_devin_client.py`) | Devin baseline | 2026-08-09, 23 minutes after it landed |
| `.cursor/environment.json` dev environment | Cursor | 2026-09-28, in the Droid refactor |
| Cursor SDK client (`app/cursor_client.py`, `tests/test_cursor_client.py`) | Cursor | 2026-09-28, in the Droid refactor |
| `/api/cursor/environments` endpoint (`tests/test_environments.py`) | Cursor | 2026-09-28, replaced by `/api/droid/models` |
| Bugbot trigger (`bugbot_trigger_on_pr`, BUGBOT_REVIEW runtime tracking) | Cursor | 2026-09-28, replaced by the in-process Droid review |
| Per-repo `cursor_environment` setting | Cursor | 2026-09-28, replaced by `droid_model` + `setup_command` |
| `Cursor-complete` trigger label | Cursor | 2026-09-28, renamed `Droid-complete` |

## Releases

There are no tags (`git tag` returns nothing). The version in code is 2.0.0 (`app/__init__.py`), which matches the v2 schema rename noted in the README: old v1 SQLite databases are not migrated.
