# Configuration

All settings come from one pydantic-settings model, `Settings` in `app/config.py`. It is configured with `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`, so values are read from the process environment with `.env` in the working directory as the fallback file, and unknown keys are ignored. Field names map to upper-case environment variables (`app_name` reads `APP_NAME`). Access is through `get_settings()`, which is wrapped in `lru_cache`.

The repository ships `.env.example` as the template; copy it to `.env`. Docker users should note that `Dockerfile` and `docker-compose.yml` set `DATABASE_URL` and `WORKSPACE_ROOT` to container paths, and `.env` is loaded as the compose `env_file`.

## What is required

Every setting has a default, so the service starts with an empty `.env`, logs warnings, and reports `degraded` on `/health` rather than failing. The columns below mark what the automation needs to actually do work.

- `GITHUB_TOKEN` is required for any GitHub write: cloning and pushing branches, opening pull requests, posting comments, and posting reviews. Without it, GitHub calls that need a token are skipped with a warning or raise `RuntimeError("GitHub token is not configured")`.
- `FACTORY_API_KEY` is required, unless `DROID_ALLOW_CLI_AUTH=true` and the `droid` CLI is on `PATH`, in which case the SDK uses the CLI's own login. `DroidClient.configured` implements exactly this rule.
- `GITHUB_WEBHOOK_SECRET` is required if webhooks are used and you want them verified. When it is empty, `verify_signature` skips the HMAC check and logs `webhook.signature_skipped`.
- `PUBLIC_BASE_URL` is required for the label-triggered webhook flow, because GitHub must be able to reach `POST /webhook`. The dashboard Assign flow works without it.

## Application

| Variable | Purpose | Default | Required |
| --- | --- | --- | --- |
| `APP_NAME` | Service name in settings and the FastAPI title | `droid-ai-engineer` | Optional |
| `APP_VERSION` | Version string reported by the app | `2.0.0` | Optional |
| `LOG_LEVEL` | Root logging level for structured logs | `INFO` | Optional |

## Database

| Variable | Purpose | Default | Required |
| --- | --- | --- | --- |
| `DATABASE_URL` | SQLAlchemy URL. SQLite paths get their parent directory created on startup | `sqlite:///./data/tasks.db` | Optional (needed to persist; in-memory works for tests) |

## GitHub

| Variable | Purpose | Default | Required |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | Personal access token used to clone and push, open same-repo PRs, comment, label, and post reviews | empty | Yes, for automation |
| `GITHUB_WEBHOOK_SECRET` | Shared secret for `X-Hub-Signature-256` validation | empty | Yes for verified webhooks |
| `GITHUB_API_URL` | REST base URL | `https://api.github.com` | Optional |
| `PUBLIC_BASE_URL` | Public tunnel URL GitHub can reach, for example an ngrok or cloudflared host. Enables webhook install and `POST /api/webhooks/sync` | empty | Yes for the label flow |

## Droid runtime

| Variable | Purpose | Default | Required |
| --- | --- | --- | --- |
| `FACTORY_API_KEY` | Factory API key read by `droid-sdk` | empty | Yes, or enable CLI auth |
| `DROID_ALLOW_CLI_AUTH` | When true and `FACTORY_API_KEY` is empty, use the local `droid` CLI's login. Intended for local runs; set the key in Docker | `false` | Optional |
| `DROID_MODEL` | Model id for new sessions. `auto` means Factory Router. A per-repo value overrides it | `auto` | Optional |
| `DROID_AUTONOMY` | Autonomy for fix sessions: `off`, `low`, `medium`, `high`. Needs `high` for branch pushes | `high` | Optional |
| `DROID_REVIEW_AUTONOMY` | Autonomy for review sessions. Do not set `off`, because headless turns auto-reject permission requests and ordinary read commands would abort the run | `high` | Optional |
| `DROID_TURN_TIMEOUT_SECONDS` | Wall-clock budget for a single turn (fix, review, follow-up, recovery) | `3600` | Optional |
| `WORKSPACE_ROOT` | Directory holding per-task clones (`<owner>/<repo>/task-<id>`, `review-<id>`) | `./data/workspace` | Optional |

## Automation behaviour

| Variable | Purpose | Default | Required |
| --- | --- | --- | --- |
| `TRIGGER_LABEL` | Issue label that starts a task | `Droid-complete` | Optional |
| `POLL_INTERVAL_SECONDS` | Interval between GitHub PR and merge state reconciliation passes | `20` | Optional |
| `REVIEW_AUTO_MERGE` | Squash-merge (or enable GitHub auto-merge) after an approving Droid review. Leave false so humans can review | `false` | Optional |

## ROI assumptions

These feed the leadership metrics in `app/metrics.py` and do not affect agent behavior. Time-to-dollar math divides the annual cost by 1920 working hours per year.

| Variable | Purpose | Default | Required |
| --- | --- | --- | --- |
| `JUNIOR_SWE_ANNUAL_COST_USD` | Assumed fully-loaded junior engineer cost | `150000` | Optional |
| `JUNIOR_HOURS_PER_ISSUE` | Hours a junior would spend per fix | `4.0` | Optional |
| `JUNIOR_HOURS_PER_REVIEW` | Hours a junior would spend per review | `1.0` | Optional |
| `DROID_USD_PER_AGENT_RUN` | Flat USD attributed per completed run when real Factory credit usage is unavailable. Set `0` to ignore | `0` | Optional |

## Related pages

- [Getting started](../overview/getting-started.md) for the setup order and a minimal `.env`.
- [Data models](data-models.md) for the `Repository` fields the dashboard edits.
- [Deployment](../deployment.md) for how the container overrides `DATABASE_URL` and `WORKSPACE_ROOT`.
- [Migration from Cursor](../background/migration-from-cursor.md) for the Cursor-era variable names these replaced.
