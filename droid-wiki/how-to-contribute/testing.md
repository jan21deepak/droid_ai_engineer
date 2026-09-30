# Testing

The suite is `pytest -q`: 85 tests in 9 files, fully offline, green in seconds. Nothing in it talks to GitHub or starts a Droid session.

## Running it

```bash
pytest -q                          # everything
pytest tests/test_worker.py -q     # one file
pytest -q -k autonomy              # by keyword
```

## Configuration

`pytest.ini` holds two lines that matter:

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

`asyncio_mode = auto` means async test functions run without a decorator. Some tests still carry `@pytest.mark.asyncio` from before; both forms work.

## The conftest fixtures

`tests/conftest.py` pins the environment at import time: `DATABASE_URL` points at a throwaway SQLite file, `GITHUB_WEBHOOK_SECRET` is `test-secret`, `GITHUB_TOKEN` and `FACTORY_API_KEY` are empty, `DROID_ALLOW_CLI_AUTH` is `false`, `TRIGGER_LABEL` is `Droid-complete`, and `WORKSPACE_ROOT` sits under `./data/test-workspace`.

Two fixtures do the real work:

- `clean_db` (autouse) gives every test an isolated database. It clears the pydantic settings cache (`get_settings.cache_clear()`), resets the `DroidClient` singleton (`reset_droid_client_for_tests()`), points `DATABASE_URL` at a fresh temp file, rebuilds the engine (`database.reset_for_tests()`), and runs `init_db()`. After the test it drops all tables and resets again. A test cannot see another test's rows, and a settings change in one test cannot leak into the next.
- `client` wraps the FastAPI app in `TestClient` inside a context manager, so lifespan startup runs (migrations, session recovery, worker loop) and shutdown drains it. Use it for every endpoint test.

## What is stubbed, and why it stays offline

Two facts keep the suite offline. First, `FACTORY_API_KEY` is empty and `DROID_ALLOW_CLI_AUTH` is `false`, so `DroidClient.configured` is always False in tests and no session can open. Second, every collaborator that would do real work is replaced at the module boundary. Tests stub classes, not HTTP. That keeps them fast and deterministic, and it forces the seams to stay clean: if a test needs to monkeypatch deep inside a function, the function is doing too much.

## respx for HTTP

The one place real HTTP objects appear is `tests/test_github.py`, and even there the network is fake. `respx` intercepts httpx calls:

```python
@pytest.mark.asyncio
@respx.mock
async def test_create_pull_request_review_posts_body_and_event():
    route = respx.post(f"{API}/repos/o/r/pulls/1/reviews").mock(
        return_value=httpx.Response(200, json={"id": 5, "state": "COMMENTED"})
    )
    ...
    assert route.called
    assert b'"event":"COMMENT"' in route.calls.last.request.content
```

Assert on the request that was sent, not just the return value. This test exists because the review-posting call once passed a reserved keyword to `log_event` and raised `TypeError` after GitHub had already accepted the review.

## StubGitHub and StubDroid

`tests/test_worker.py` holds the two stubs to copy for worker-path tests:

- `StubGitHub` records calls instead of making them: `comments`, `created_prs`, `review_posts`. Its `merge_pull_request` and `enable_auto_merge` raise `AssertionError`, which is how the suite proves auto-merge stays off by default. A subclass (`OwnPrGitHub`) raises `httpx.HTTPStatusError` with a 422 to simulate GitHub rejecting an approval on the token owner's own PR.
- `StubDroid` stands in for `DroidClient`: it answers `current_branch` with a fixed branch, records pushes, and reports no active sessions.

Tests wire them in with `monkeypatch.setattr(worker, "GitHubClient", lambda: github)` and `monkeypatch.setattr(worker, "get_droid_client", lambda: droid)`, then call `finalize_fix_task`, `finalize_review_task`, `poll_github_state`, or `recover_interrupted_tasks` directly with a hand-built outcome dict (`success_outcome()` and `failure_outcome()` in the same file are the templates).

For SDK-adjacent code, `tests/test_droid_client.py` has `FakeSession` and `FakeStream`: the fake session's `stream()` returns an async context manager that yields a few message events and then exposes a `SimpleNamespace` result with `success`, `text`, `duration`, `usage`. Workspace prep is tested with real git in `tmp_path`, which is how the merge-base regression test works.

## Adding a test

For a new endpoint:

1. Take the `client` fixture.
2. Insert any rows you need through `db_session()` from `app/database.py`.
3. Call the route and assert the status code and body. For webhook deliveries, sign the body like `tests/test_webhook.py` does: `"sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()` under `X-Hub-Signature-256`, with `test-secret` as the secret.

For a new worker path:

1. Insert task or review rows with `db_session()`.
2. Build the outcome dict the `DroidClient` callback would receive.
3. Stub collaborators with `monkeypatch` (extend `StubGitHub`/`StubDroid` rather than inventing new fakes).
4. Call the function directly, then reopen a session and assert the persisted state, including the failure branches.

Never add a real network call or a real Droid session. Extend a stub instead.

## The files

| File | Lines | What it covers |
| --- | --- | --- |
| `tests/conftest.py` | 46 | Environment pinning, `clean_db`, `client` |
| `tests/test_health.py` | 21 | Root redirect, dashboard HTML, `/health` component reporting |
| `tests/test_webhook.py` | 126 | HMAC verification, issue event parsing, trigger rules, endpoint 401/202/200/422 |
| `tests/test_github.py` | 38 | PR review posting through respx, error propagation |
| `tests/test_droid_client.py` | 404 | Prompt builders, branch/verdict extraction, workspace prep (clone reuse, merge base, review autonomy), session lifecycle |
| `tests/test_worker.py` | 458 | Fix and review finalization, PR reuse, COMMENT fallback, restart recovery, PR state polling |
| `tests/test_runtime.py` | 58 | Runtime formatting, PR number parsing, completion comment rendering |
| `tests/test_models_endpoint.py` | 76 | `/api/droid/models`, per-repo launch config, repository add and patch |
| `tests/test_metrics.py` | 545 | Dashboard and metrics aggregates, KPI math, SGT day bucketing, activity pagination |

## Related pages

- [Patterns and conventions](patterns-and-conventions.md) for the rules the tests enforce.
- [Debugging](debugging.md) for when a green suite is not enough.
- [Development workflow](development-workflow.md) for the end-to-end validation the suite cannot replace.
