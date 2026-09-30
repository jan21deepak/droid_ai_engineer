# Security

Droid Forge has two inbound surfaces and two outbound capabilities. Inbound: the GitHub webhook, and the HTTP API plus dashboard. Outbound: a GitHub personal access token, and local Droid subprocesses with git push rights. The sections below describe each boundary.

## Webhook verification

GitHub deliveries to `POST /webhook` are verified before any parsing or task creation. `verify_signature` in `app/github.py` recomputes the HMAC SHA-256 digest of the raw body with `GITHUB_WEBHOOK_SECRET` and compares it to `X-Hub-Signature-256` with `hmac.compare_digest`; a mismatch returns 401. When forge installs webhooks itself (`ensure_issues_webhook`), it configures `content_type: json` and `insecure_ssl: "0"` on the GitHub side.

One caveat: when `GITHUB_WEBHOOK_SECRET` is empty, verification is skipped and every delivery is accepted (`verify_signature` returns True and logs `webhook.signature_skipped`). Treat the secret as required in any deployment where the port is reachable.

## The GitHub token

`GitHubClient` in `app/github.py` reads `GITHUB_TOKEN` from settings and sends it as a Bearer header on every REST call. `.env.example` describes it as a personal access token with `repo` scope. With it the service can clone and push branches, open same-repo pull requests, add labels, install webhooks, create issues, comment, post reviews, and (when `REVIEW_AUTO_MERGE` is on) merge. Grant it the narrowest scope that covers the registered repositories.

## Token embedding in clone remotes

`_remote_url` in `app/droid_client.py` embeds the token in every workspace clone's origin remote as `https://x-access-token:<token>@github.com/owner/repo.git`, so the agent can push its branch. Two consequences:

- The token lives in the git config of every workspace under `WORKSPACE_ROOT`.
- Git error output echoes remote URLs. The `_git` helper includes stderr (truncated to 500 characters) in its error text, and those errors flow into log fields such as `error=`. Treat logs, task rows and agent transcripts as secret-bearing, and keep the host and volumes private.

## Secret handling

Settings load from environment variables and a `.env` file through `app/config.py`. `.gitignore` excludes `.env`, and `docker-compose.yml` passes it to the container with `env_file`. The code does not log tokens and does not write them to the database.

## The Droid runtime

Droid sessions run as local subprocesses inside per-task clones (`app/droid_client.py`). What a session can do is bounded by configuration, not by a sandbox:

- `DROID_AUTONOMY` controls fix sessions (`off`, `low`, `medium`, `high`; the default `high` allows git push). `DROID_REVIEW_AUTONOMY` controls review sessions and must not be `off`: headless runs auto-reject permission requests (`auto_reject_permission_requests=True` in `_session_config`), so `off` aborts the review on ordinary read commands.
- A per-repo `setup_command` runs an arbitrary shell command in the clone before the session starts. Registering a repository is therefore a privileged action, and the JSON surface has no authentication of its own, so only trusted users should reach the port.
- Fix sessions are prompted to create a branch, run tests, and push to `origin` only, never to a parent repository. Review sessions get an explicit read-only instruction: "Do not modify files, push branches, or open pull requests. Review only."

The prompts are guidance, not isolation. A session runs with the container user's permissions and the PAT's push rights; the host is the real security boundary.

## Review posting fallback

Reviews are posted with `create_pull_request_review` in `app/github.py` as `APPROVE` or `REQUEST_CHANGES`, based on the agent's `VERDICT:` line. GitHub rejects both on pull requests authored by the token owner, so `finalize_review_task` in `app/worker.py` catches 403 and 422 and retries with `event="COMMENT"`. The verdict still appears in the review body, but GitHub shows no formal approve or request-changes state. When you need a formal approval, the PR must be authored by a different account than the token owner.

## Secrets

| Secret | Configured by | Used for |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | env | HMAC check in `verify_signature` (`app/github.py`); set as the webhook secret when forge installs hooks |
| `GITHUB_TOKEN` | env | Bearer auth for all GitHub REST calls (`app/github.py`); embedded in workspace clone remotes (`app/droid_client.py`) |
| `FACTORY_API_KEY` | env | Agent auth, passed to droid-sdk sessions and `list_models` (`app/droid_client.py`) |
| Droid CLI login | `DROID_ALLOW_CLI_AUTH=true` with an empty key | SDK fallback to the CLI's stored login; local runs only, see [Deployment](deployment.md) |
