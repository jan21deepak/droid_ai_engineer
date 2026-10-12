"""Application configuration loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

ACCESS_MODE_PROTECTED = "protected"
ACCESS_MODE_LOCAL_DEV = "local_dev"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "droid-ai-engineer"
    app_version: str = "2.0.0"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./data/tasks.db"

    github_webhook_secret: str = ""
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    # Public base URL GitHub can reach (ngrok / cloudflared), e.g. https://abc.trycloudflare.com
    # When set, forge installs issue webhooks on registered repos and Assign can rely on label→webhook.
    public_base_url: str = ""

    # Droid (Factory) agent runtime. The official Python SDK (`droid-sdk`) runs
    # local `droid` sessions; the SDK reads FACTORY_API_KEY from here (or from
    # the CLI's own auth when the key is empty and DROID_ALLOW_CLI_AUTH is on).
    factory_api_key: str = ""
    # When True and FACTORY_API_KEY is empty, use the local droid CLI's own
    # authentication (requires `droid` on PATH). Intended for local runs; in
    # Docker always set FACTORY_API_KEY.
    droid_allow_cli_auth: bool = False
    # Model id for new sessions; "auto" = Factory Router. Overridable per repo.
    droid_model: str = "auto"
    # Autonomy for fix sessions: off | low | medium | high. Review sessions do
    # not use autonomy; they are restricted to Read/Grep/Glob (app.review_policy),
    # so a legacy DROID_REVIEW_AUTONOMY value is ignored.
    droid_autonomy: str = "high"
    # Wall-clock budget for a single Droid turn (fix, review, follow-up)
    droid_turn_timeout_seconds: float = 3600.0
    # Where per-task repository workspace clones live
    workspace_root: str = "./data/workspace"

    poll_interval_seconds: int = 20
    trigger_label: str = "Droid-complete"

    # Optional error tracking (Sentry). Empty = disabled (local/dev default).
    sentry_dsn: str = ""
    sentry_environment: str = "local"

    # Optional product analytics (PostHog). Empty = disabled; events are
    # fire-and-forget and never block or fail product flows.
    posthog_key: str = ""
    posthog_host: str = "https://us.i.posthog.com"

    # There is deliberately no auto-merge setting: Forge never merges or enables
    # GitHub auto-merge, and a legacy REVIEW_AUTO_MERGE value is ignored.

    # Operator access. Anything other than the literal "local_dev" is treated
    # as protected so a typo can never silently disable authentication.
    forge_access_mode: str = ACCESS_MODE_PROTECTED
    # Shared single-operator credential (login form or `Authorization: Bearer`).
    forge_operator_token: str = ""
    # Absolute lifetime of a browser session created by /login.
    forge_session_ttl_seconds: int = 8 * 3600
    # Comma-separated Host header values the app answers to (DNS-rebinding and
    # spoofed-host defense). PUBLIC_BASE_URL's host is added automatically.
    forge_trusted_hosts: str = "127.0.0.1,localhost,::1"
    # Session cookie Secure flag: auto (Secure except plain-HTTP loopback) | true | false
    forge_cookie_secure: str = "auto"
    # Comma-separated owner/repo slugs approved for intake and automation.
    forge_allowed_repositories: str = ""
    # Comma-separated branch names the app may push to or open PRs from.
    # Empty approves nothing in protected mode (any branch in local_dev).
    forge_allowed_branches: str = ""
    # Master switch for every GitHub write (comments, labels, issues, PRs,
    # reviews, pushes). Off by default so nothing is published unapproved.
    forge_allow_github_writes: bool = False
    # Stored per-repository setup commands only execute with this opt-in.
    forge_allow_privileged_setup: bool = False

    # ROI / productivity assumptions (leadership dashboard)
    junior_swe_annual_cost_usd: float = 150_000.0
    junior_hours_per_issue: float = 4.0
    junior_hours_per_review: float = 1.0
    # Flat estimated USD attributed per completed Droid run when real Factory
    # credit usage is unavailable (set 0 to ignore).
    droid_usd_per_agent_run: float = 0.0
    # Optional explicit credit-to-USD *estimate* applied to attempts with observed
    # credits. Ignored unless both a finite positive rate and a source label are
    # set; no conversion is ever assumed, and estimates are never shown as bills.
    # Kept as text so an empty or malformed value is ignored instead of failing startup.
    forge_usd_per_credit_estimate: str = ""
    forge_usd_estimate_source: str = ""

    # Run limits (app.run_limits). At most this many paid Droid turns run at
    # once across implementation, review, follow-up and recovery.
    forge_max_concurrent_runs: int = 1
    # Follow-up + recovery turns allowed per implementation task.
    forge_max_continuations: int = 3
    # Spending check applied before every paid turn: none | account_cap | credit_limit.
    # Any other value blocks every paid turn (fail closed).
    forge_budget_policy: str = "none"
    # account_cap: the account-level USD cap the account owner confirmed is
    # enforced by Factory billing. Forge records it; it does not enforce dollars.
    forge_demo_account_cap_usd: str = ""
    # credit_limit: Factory credits Forge may observe being used before it blocks new turns.
    forge_credit_limit: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def effective_access_mode(settings: Settings) -> str:
    """Resolve the access mode, failing closed to protected on any unknown value."""
    if (settings.forge_access_mode or "").strip().lower() == ACCESS_MODE_LOCAL_DEV:
        return ACCESS_MODE_LOCAL_DEV
    return ACCESS_MODE_PROTECTED


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").replace("\n", ",").split(",") if item.strip()]
