# Cursor Forge — The AI Engineer That Delivers

A production-ready automation service that turns **GitHub Issues into merged Pull Requests** using the official [Cursor SDK](https://cursor.com/docs/sdk/python) (`cursor-sdk`) against [Cursor Cloud Agents](https://cursor.com/docs/cloud-agent). Label an issue, and the system dispatches a Cursor agent to implement the fix, run the tests, open a PR, review it with a second Cursor agent, and report back on the issue — with full lifecycle tracking, metrics, and an operations dashboard.

Built for **fintech / digital-banking** engineering teams evaluating autonomous remediation on business-critical codebases (demo target: a fork of [`apache/fineract`](https://github.com/apache/fineract)).

---

## Project Overview

When a GitHub Issue is labeled `Cursor-complete` in a configured repository (e.g. your fork of [`apache/fineract`](https://github.com/apache/fineract)), this service:

1. Receives the webhook and validates its HMAC signature.
2. Persists a **Task** record in SQLite.
3. Creates a **Cursor Cloud Agent** via the Python SDK (`Agent.create` + `agent.send`) with `CloudAgentOptions`. When the registered repository has a **Cursor environment** name set, the agent launches with `CloudEnvironment(name=…)` so it uses that OSS repo’s configured environment (install/snapshot/secrets) instead of a bare clone.
4. Polls agent runs every 20 seconds (`Agent.get_run` / `Agent.list_runs`) in a background worker.
5. On completion, stores the **pull request URL, summary, and runtime**, comments ``bugbot run`` so **Cursor Bugbot** reviews the PR, and posts a ✅ comment back on the original issue.
6. Exposes a live **dashboard**, a **metrics endpoint**, an orchestration-grade **health endpoint**, and a **follow-up** API (`POST /api/tasks/{id}/follow-up`) for SDK `Agent.resume` + `send`.

## Architecture

Issue-fix Cloud Agents launch in the **Cursor environment configured for that repository** (e.g. Omnigent issues → Omnigent env, Superset → Superset env), not the forge app’s own environment. Each registered repo stores a `cursor_environment` name in SQLite; create uses `CloudEnvironment(name=…)` from the Cursor SDK.

![Cursor Forge architecture](docs/architecture.png)

```mermaid
flowchart LR
    ISSUE["GitHub Issue<br/>labeled Cursor-complete"]
    WEBHOOK["Cursor Forge FastAPI<br/>POST /webhook / Assign"]
    DB[("SQLite<br/>Task + Repo store<br/>cursor_environment per repo")]
    ENV["Per-repo Cursor Environment<br/>Omnigent / Superset / …"]
    API["Cursor SDK<br/>CloudEnvironment + Agent.create"]
    SESSION["Cursor Cloud Agent<br/>plan, code, test<br/>in that repo's env"]
    WORKER["Background Worker<br/>polls SDK runs"]
    PR["Pull Request<br/>autoCreatePR or PAT fallback"]
    REVIEW["Cursor Review Agent<br/>on PR"]
    COMMENT["GitHub Comment<br/>issue + merge tracking"]
    DASH["Dashboard<br/>GET /dashboard"]
    METRICS["Metrics<br/>GET /metrics"]

    ISSUE -->|"webhook / assign"| WEBHOOK
    WEBHOOK -->|"persist Task"| DB
    DB -->|"lookup cursor_environment"| ENV
    WEBHOOK -->|"Agent.create + send"| API
    ENV -->|"CloudEnvironment name"| API
    API --> SESSION
    SESSION -->|"opens"| PR
    PR --> REVIEW
    SESSION -->|"on complete"| COMMENT
    WORKER -->|"get_run / list_runs"| API
    WORKER -->|"update"| DB
    DB -.-> DASH
    DB -.-> METRICS
```

Source: [`docs/architecture.mmd`](docs/architecture.mmd). To regenerate the PNG:

```bash
npx -p @mermaid-js/mermaid-cli mmdc \
  -i docs/architecture.mmd \
  -o docs/architecture.png \
  -b "#0b1220" -w 1500 -s 1
```

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI + Uvicorn |
| Persistence | SQLite + SQLAlchemy 2.x |
| Background worker | asyncio task (in-process, 20s poll loop) |
| Agent runtime | Official Python [Cursor SDK](https://cursor.com/docs/sdk/python) (`cursor-sdk`) → Cloud Agents |
| HTTP client | httpx (async) for GitHub; SDK for Cursor agents |
| Config | pydantic-settings + python-dotenv |
| UI | Jinja2 + Bootstrap 5 |
| Container | Docker + Docker Compose |
| Tests | pytest + respx |

---

## Clone and replicate this setup

Follow these steps on any machine (macOS/Linux) to run the same stack end-to-end.

### Prerequisites

Install before you start:

| Tool | Why |
|---|---|
| [Git](https://git-scm.com/) | Clone the repo |
| [Python 3.12+](https://www.python.org/downloads/) | Runtime |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) *(optional)* | One-command container run |
| [GitHub CLI `gh`](https://cli.github.com/) *(recommended)* | Create webhooks / test issues |
| [ngrok](https://ngrok.com/) or [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) | Expose `localhost:8000` so GitHub can reach `/webhook` |

You will also need:

1. A **GitHub personal access token** with permission to read repos, write issues/comments, and merge PRs on the target repository.
2. A **Cursor API key** from [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations) (or a team service-account key). The key must be allowed to launch Cloud Agents on the target GitHub repos (install the Cursor GitHub app on those repos).
3. A **webhook secret** string of your choosing (any long random value).

### 1. Clone the repository

```bash
git clone https://github.com/jan21deepak/cursor_ai_engineer.git
cd cursor_ai_engineer
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in at least:

```bash
GITHUB_WEBHOOK_SECRET=some-long-random-secret
GITHUB_TOKEN=<your-github-pat>
CURSOR_API_KEY=<your-cursor-api-key>
TRIGGER_LABEL=Cursor-complete
```

Optional: `CURSOR_MODEL` (default `composer-2.5`), `CURSOR_STARTING_REF` (default `main`), `CURSOR_USD_PER_AGENT_RUN` for ROI estimates.

Verify the Cursor key (SDK uses the same key):

```bash
python - <<'PY'
from cursor_sdk import Cursor
import os
print(Cursor.me(api_key=os.environ["CURSOR_API_KEY"]))
print([m.id for m in Cursor.models.list(api_key=os.environ["CURSOR_API_KEY"])][:5])
PY
```

### 3. Run the service

**Option A — Docker (recommended for a clean replicate)**

```bash
docker compose up --build
```

This starts FastAPI on **http://localhost:8000**, initializes SQLite at `/data/tasks.db` inside the container, and persists it in the `cursor_data` volume.

**Option B — Local Python**

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

mkdir -p data
.venv/bin/uvicorn app.app:app --host 0.0.0.0 --port 8000
```

In another terminal (with the venv activated):

```bash
pytest -q
```

### 4. Verify the app is healthy

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
curl -s http://localhost:8000/metrics | python3 -m json.tool
open http://localhost:8000/dashboard   # or visit in a browser
```

Healthy response looks like:

```json
{
  "status": "healthy",
  "application": "ok",
  "checks": { "database": "ok", "github": "ok", "cursor": "ok" }
}
```

If `github` or `cursor` show as unreachable/unconfigured, the app still serves traffic (`degraded`) — fix the tokens in `.env` and restart.

### 5. Expose a public webhook URL

GitHub cannot call `localhost`. Tunnel it:

```bash
# ngrok
ngrok http 8000
# → copy the https://….ngrok-free.app URL

# or cloudflared
cloudflared tunnel --url http://localhost:8000
```

### 6. Point a GitHub webhook at your tunnel

Replace `<owner>/<repo>` with the repository you want Cursor to work on, and `<public-host>` with the tunnel host from step 5:

```bash
gh auth login   # if needed — use the same account that owns the repo

gh api repos/<owner>/<repo>/hooks -f name=web \
  -F "config[url]=https://<public-host>/webhook" \
  -F "config[content_type]=json" \
  -F "config[secret]=$GITHUB_WEBHOOK_SECRET" \
  -F "events[]=issues" \
  -F active=true
```

Or in the GitHub UI: **Settings → Webhooks → Add webhook**

- Payload URL: `https://<public-host>/webhook`
- Content type: `application/json`
- Secret: same value as `GITHUB_WEBHOOK_SECRET`
- Events: **Issues** only

### 7. Register the repo in the dashboard and trigger Cursor

1. Open **http://localhost:8000/dashboard** → **Repositories** tab.
2. Paste `https://github.com/<owner>/<repo>` and click **Add repository**.
3. Click **Add Issues** (imports open issues from a parent fork when applicable) or **Sync**.
4. On the **Issues** tab, select issues → **Assign to Cursor**.

**Or trigger via the webhook label:**

```bash
# Create the label once
gh label create Cursor-complete --repo <owner>/<repo> \
  --color 0E8A16 --description "Trigger Cursor Forge automation" || true

# Open / label an issue
gh issue create --repo <owner>/<repo> \
  --title "Fix: example bug" \
  --body "Describe the change." \
  --label Cursor-complete
```

### 8. Watch the result

- **Dashboard** tab: fix + review stats, engineering KPIs, daily activity chart (times in **SGT**).
- Original GitHub issue: ✅ completion comment with agent ID, PR URL, runtime, summary.
- Cursor opens a PR (`autoCreatePR`); a review Cloud Agent is started against that PR; merge state is refreshed from GitHub.

Deep links open in the Cursor UI: `https://cursor.com/agents/{agentId}`.

---

## How It Works

### GitHub Webhook Flow

1. GitHub delivers an `issues` event to `POST /webhook`.
2. The service verifies `X-Hub-Signature-256` with the shared secret (constant-time HMAC compare). Invalid → **401**.
3. Non-issue events are acknowledged and ignored (**200**).
4. Trigger rule: issue `opened` with the `Cursor-complete` label, or the `Cursor-complete` label being added (`labeled`). Anything else is ignored.
5. Duplicate protection: an active or completed task for the same repo + issue is not recreated.
6. A `Task` row is written to SQLite, and a Cursor Cloud Agent is created (**202**).

### Cursor Cloud Agent Workflow

The agent prompt includes the repository URL, issue title, and issue body, and instructs Cursor to:

- make only the requested changes,
- run the project's test suite,
- fix any failures introduced,
- push a branch on **this** repository (the fork),
- do **not** open a PR against an upstream / parent repo,
- summarize the completed work and branch name.

Create path (Python Cursor SDK, simplified). When the registered repo has a Cursor environment name, forge selects that named env (mutually exclusive with an explicit `repos` list for fix agents). Issue-fix agents use `auto_create_pr=False`; the forge worker opens a **same-repo** PR on the fork with `GITHUB_TOKEN` (`head=owner:branch`) so Cursor never targets the upstream parent:

```python
from cursor_sdk import Agent, CloudAgentOptions, CloudEnvironment, CloudRepository

# Issue fix — use the OSS repo's Cursor Cloud Agents environment
agent = Agent.create(
    model="composer-2.5",
    api_key=CURSOR_API_KEY,
    cloud=CloudAgentOptions(
        env=CloudEnvironment(type="cloud", name="omnigent"),  # per-repo env
        auto_create_pr=False,  # forge opens within-fork PRs via PAT
        skip_reviewer_request=True,
    ),
)
run = agent.send(prompt)  # fire-and-forget; worker polls later

# Fallback when no env is configured: bare clone of the target repo
# cloud=CloudAgentOptions(
#     repos=[CloudRepository(url="https://github.com/owner/repo", starting_ref="main")],
#     auto_create_pr=False,
#     skip_reviewer_request=True,
# )
```

The background worker polls every 20 seconds via the SDK:

- `Agent.get_run(run_id, {"runtime": "cloud", "agentId": ...})` (or `Agent.list_runs`)
- Reads status, `result`, `duration_ms`, `git.branches[].pr_url` / branch name
- Opens a **same-repo** PR on the configured fork with the forge `GITHUB_TOKEN` (`head=owner:branch`, base = fork default / starting_ref)

Status map: `creating`/`running` → queued/running; `finished` → completed; `error`/`cancelled`/`expired` → failed.

On success it posts a comment to the issue and comments ``bugbot run`` on the PR so **Cursor Bugbot** reviews it. Forge no longer starts a separate Cursor Cloud **review agent**, and auto-merge stays off (`REVIEW_AUTO_MERGE=false`).

Follow-ups use `Agent.resume(agent_id)` + `agent.send(...)` via `POST /api/tasks/{id}/follow-up`.

Configure each repo’s environment name on the dashboard (**Repositories** → Cursor environment → Save), matching the name in [Cursor Cloud Agents](https://cursor.com/dashboard/cloud-agents).

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | Shared secret for webhook HMAC validation | — |
| `GITHUB_TOKEN` | PAT used to post comments / open same-repo PRs | — |
| `REVIEW_AUTO_MERGE` | Squash-merge after Cursor review finishes (`true`/`false`) | `false` |
| `BUGBOT_TRIGGER_ON_PR` | Comment `bugbot run` on forge PRs for Cursor Bugbot | `true` |
| `CURSOR_API_KEY` | Cursor user or service-account API key | — |
| `CURSOR_API_BASE` | Cursor API base URL | `https://api.cursor.com` |
| `CURSOR_MODEL` | Model id passed to `Agent.create` | `composer-2.5` |
| `CURSOR_NAME_PREFIX` | Prefix for agent display names | `cursor-forge` |
| `CURSOR_STARTING_REF` | Default git ref for fix agents | `main` |
| `DATABASE_URL` | SQLAlchemy URL | `sqlite:///./data/tasks.db` |
| `POLL_INTERVAL_SECONDS` | Worker poll interval | `20` |
| `TRIGGER_LABEL` | Issue label that triggers automation | `Cursor-complete` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `JUNIOR_SWE_ANNUAL_COST_USD` | Assumed junior SWE fully-loaded cost | `150000` |
| `JUNIOR_HOURS_PER_ISSUE` | Hours a junior would spend per fix | `4.0` |
| `JUNIOR_HOURS_PER_REVIEW` | Hours a junior would spend per review | `1.0` |
| `CURSOR_USD_PER_AGENT_RUN` | Flat USD attributed per completed agent run | `0` |

Time-to-Dollar Savings divides annual cost by **1,920** working hours/year. The service boots without API keys, logs warnings, and reports `degraded` on `/health`.

## Dashboard

`GET /dashboard` — neon dark UI, auto-refreshes every 15 seconds while the Dashboard tab is active:

- Repositories / Issues / Dashboard tabs
- Fix + Review time stats (active Cursor work)
- Engineering KPIs (avg PR cycle time, PRs delivered, merge rate, change failure rate)
- Daily activity bar chart (last 14 days, SGT)
- Recent activity with Cursor Agent / Cursor Review deep links

## Metrics Endpoint

`GET /metrics` — computed live from SQLite (fix/review buckets, engineering KPIs, daily series, ROI assumptions). Cost uses stored `cost_usd` / `CURSOR_USD_PER_AGENT_RUN`.

## Health Endpoint

`GET /health` — suitable for orchestration platforms (Kubernetes, ECS, Docker healthchecks):

```json
{
  "status": "healthy",
  "application": "ok",
  "checks": { "database": "ok", "github": "ok", "cursor": "ok" }
}
```

- **200** — `healthy`, or `degraded` (external API unreachable/unconfigured; app still serves traffic)
- **503** — `unhealthy` (database unreachable)

## Observability

Structured single-line logs with ISO-8601 **SGT** timestamps and key=value context, covering: webhook received, Cursor agent created, polling started/completed, task status changes, task completed/failed, GitHub comment posted, and database updates.

```
2026-08-03T11:30:00.123+08:00 | INFO | app.worker | task.completed | task_id=7 agent_id=bc-… pr=https://github.com/… runtime_seconds=734
```

## Cursor SDK (Cloud Agents)

| Concept | Cursor Forge usage |
|---|---|
| Cloud Agent | Durable agent id (`bc-…`) via `Agent.create(..., cloud=...)` |
| Run | Per-prompt execution via `agent.send(...)` |
| Per-repo environment | Registered repo `cursor_environment` → `CloudEnvironment(type="cloud", name=…)` for issue fixes |
| Create (fix) | `auto_create_pr=False` + named env or `repos=[…]`; forge opens same-repo PR on the fork |
| PR review | Cursor **Bugbot** via `bugbot run` comment (no Cloud review agent) |
| Poll | `Agent.get_run` / `Agent.list_runs` (`runtime=cloud`) |
| PR URL | Worker opens same-repo PR via PAT (`head=owner:branch` on the fork, never upstream) |
| Follow-up | `Agent.resume` + `send` (`POST /api/tasks/{id}/follow-up`) |
| UI deep link | `https://cursor.com/agents/{id}` (Filter → Source → SDK) |
| Trigger label | `Cursor-complete` |

Canonical docs: [Python Cursor SDK](https://cursor.com/docs/sdk/python).

## Future Improvements

- **Queue-based dispatch** (Celery / Redis) for horizontal scaling beyond the in-process worker
- **Postgres** backend for multi-replica deployments
- **SDK streaming** of run events into the dashboard activity feed
- **Slack / Teams notifications** on task completion
- **Auth (OIDC)** on the dashboard and metrics endpoints
- **Per-repo trigger labels and prompt templates** (environment mapping is already supported)
- **Inline MCP servers** on agent create (GitHub/Linear) for richer enterprise context
