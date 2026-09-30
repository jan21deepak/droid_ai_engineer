# Dependencies

`requirements.txt` is a single flat list with no runtime and test split, and it uses lower-bound pins (`>=`) rather than exact pins. The file lists eight runtime packages and three test packages.

## Runtime

| Package | Pin | What it provides |
| --- | --- | --- |
| `fastapi` | `>=0.115.6` | The HTTP app: route handlers, `Request` bodies, response models, and the Jinja2 template glue |
| `uvicorn[standard]` | `>=0.34.0` | The ASGI server. The app runs as one uvicorn process with an embedded worker loop |
| `sqlalchemy` | `>=2.0.36` | Declarative ORM models and the SQLite engine and session factory |
| `httpx` | `>=0.28.1` | Async HTTP client for all GitHub REST calls, and for the GraphQL request used to enable auto-merge |
| `python-dotenv` | `>=1.0.1` | `.env` parsing, used by pydantic-settings to load the env file |
| `pydantic-settings` | `>=2.7.1` | The `Settings` model in `app/config.py` with `env_file=".env"` |
| `jinja2` | `>=3.1.5` | Template rendering for the dashboard page |
| `droid-sdk` | `>=0.4.0` | The official Factory Python SDK. It provides `Session`, `SessionConfig`, `Autonomy`, and `list_models`, and drives the `droid` CLI as a subprocess |

## Test

`pytest.ini` sets `asyncio_mode = auto` and `testpaths = tests`, so async test functions run without per-test markers.

| Package | Pin | What it provides |
| --- | --- | --- |
| `pytest` | `>=8.3.4` | The test runner |
| `pytest-asyncio` | `>=0.25.2` | Async test support for the auto mode configured above |
| `respx` | `>=0.22.0` | Mocking for `httpx` requests, used by the HTTP-level tests |

`fastapi.testclient` (which pulls in `starlette` and `httpx`) and `httpx` also back the test suite. `tests/conftest.py` forces Droid into an unconfigured state (`FACTORY_API_KEY=""`, `DROID_ALLOW_CLI_AUTH=false`) so no test reaches a real runtime, and points `DATABASE_URL` and `WORKSPACE_ROOT` at temporary paths per test.

## External runtime dependencies

Two things outside the Python package list are required at runtime.

1. **The GitHub REST API** (`https://api.github.com` by default, overridable with `GITHUB_API_URL`). `app/github.py` uses it to verify and set up webhooks, read issues and pull requests, create issues and labels, post issue comments, open same-repo pull requests, post PR reviews, merge, and read PR state. The auto-merge fallback in `enable_auto_merge` posts to the GraphQL endpoint at `https://api.github.com/graphql`, which is hardcoded rather than derived from `GITHUB_API_URL`. A `GITHUB_TOKEN` with repo write scope is what makes these calls work, and `check_connectivity` probes `/rate_limit`.
2. **The Factory `droid` CLI and `droid-sdk`.** The SDK starts `droid` as a subprocess to run local sessions, so the CLI must be installed and on `PATH`. Authentication is either `FACTORY_API_KEY` or, when `DROID_ALLOW_CLI_AUTH=true` and no key is set, the CLI's own stored login. `DroidClient.configured` checks for the key, or for `shutil.which("droid")` when CLI auth is allowed. `Dockerfile` installs the CLI with `curl -fsSL https://app.factory.ai/cli | sh` and keeps sessions on disk under `$HOME/.factory`.

The `git` binary is also a runtime requirement rather than a package: `app/droid_client.py` shells out to `git` for cloning, fetching, checkout, and push, and `Dockerfile` installs it explicitly. Two further optional prerequisites for an operator are a tunneling tool (ngrok or cloudflared) to expose `POST /webhook`, and Docker for the container path.

## Related pages

- [Configuration](configuration.md) for the keys these dependencies read.
- [Getting started](../overview/getting-started.md) for the install and run sequence.
- [Testing](../how-to-contribute/testing.md) for how the test dependencies are used.
- [Deployment](../deployment.md) for how the container installs the CLI and git.
