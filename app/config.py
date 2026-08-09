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
    cursor_api_base: str = "https://api.cursor.com"
    # Optional model id from GET /v1/models; blank uses Cursor account default
    cursor_model: str = ""
    cursor_name_prefix: str = "cursor-forge"
    cursor_starting_ref: str = "main"

    poll_interval_seconds: int = 20
    trigger_label: str = "Cursor-complete"

    # ROI / productivity assumptions (leadership dashboard)
    junior_swe_annual_cost_usd: float = 150_000.0
    junior_hours_per_issue: float = 4.0
    junior_hours_per_review: float = 1.0
    # Flat estimated USD attributed per completed Cursor agent run (fix or review)
    cursor_usd_per_agent_run: float = 0.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
