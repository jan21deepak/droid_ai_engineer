"""Application configuration loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Autonomy for fix sessions: off | low | medium | high
    droid_autonomy: str = "high"
    # Autonomy for PR review sessions. Must not be "off": headless runs
    # auto-reject permission requests, and OFF makes the review agent ask for
    # permission on ordinary read commands, which aborts the run. The review
    # prompt keeps the agent read-only; this only controls prompting.
    droid_review_autonomy: str = "high"
    # Wall-clock budget for a single Droid turn (fix, review, follow-up)
    droid_turn_timeout_seconds: float = 3600.0
    # Where per-task repository workspace clones live
    workspace_root: str = "./data/workspace"

    poll_interval_seconds: int = 20
    trigger_label: str = "Droid-complete"

    # When True, forge squash-merges (or enables GitHub auto-merge) after the
    # Droid review agent approves. Leave False so humans can review too.
    review_auto_merge: bool = False

    # ROI / productivity assumptions (leadership dashboard)
    junior_swe_annual_cost_usd: float = 150_000.0
    junior_hours_per_issue: float = 4.0
    junior_hours_per_review: float = 1.0
    # Flat estimated USD attributed per completed Droid run when real Factory
    # credit usage is unavailable (set 0 to ignore).
    droid_usd_per_agent_run: float = 0.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
