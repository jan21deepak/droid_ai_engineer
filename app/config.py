"""Application configuration loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "cursor-ai-engineer"
    app_version: str = "1.0.0"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./data/tasks.db"

    github_webhook_secret: str = ""
    github_token: str = ""
    github_api_url: str = "https://api.github.com"

    cursor_api_key: str = ""
    # Retained for docs/health display; transport is owned by cursor-sdk
    cursor_api_base: str = "https://api.cursor.com"
    # Model id for Cloud Agents (SDK). Blank defaults to composer-2.5 in the client.
    cursor_model: str = "composer-2.5"
    cursor_name_prefix: str = "cursor-forge"
    cursor_starting_ref: str = "main"

    poll_interval_seconds: int = 20
    trigger_label: str = "Cursor-complete"

    # When True, forge squash-merges (or enables GitHub auto-merge) after the
    # Cursor review agent finishes. Leave False so Bugbot / humans can review.
    review_auto_merge: bool = False
    # Comment ``bugbot run`` on each forge-opened PR so Cursor Bugbot picks it up.
    bugbot_trigger_on_pr: bool = True

    # ROI / productivity assumptions (leadership dashboard)
    junior_swe_annual_cost_usd: float = 150_000.0
    junior_hours_per_issue: float = 4.0
    junior_hours_per_review: float = 1.0
    # Flat estimated USD attributed per completed Cursor agent run (fix or review)
    cursor_usd_per_agent_run: float = 0.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
