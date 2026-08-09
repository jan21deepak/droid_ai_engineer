"""Application configuration loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "devin-ai-engineer"
    app_version: str = "1.0.0"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./data/tasks.db"

    github_webhook_secret: str = ""
    github_token: str = ""
    github_api_url: str = "https://api.github.com"

    devin_api_key: str = ""
    # Service-user (cog_*) keys use the v3 org-scoped API
    devin_api_base: str = "https://api.devin.ai/v3"
    devin_org_id: str = ""
    # Without this, service-user sessions are attributed to the "bot_apk" pseudo-user
    # and show up as an unknown user in the Devin UI and audit logs. Requires the
    # service user to hold ImpersonateOrgSessions.
    devin_create_as_user_id: str = ""
    # Applied to every session so Devin Forge runs are identifiable in Devin's UI.
    devin_session_tag: str = "devin-forge"

    poll_interval_seconds: int = 20
    trigger_label: str = "Devin-complete"

    # ROI / productivity assumptions (leadership dashboard)
    junior_swe_annual_cost_usd: float = 150_000.0
    junior_hours_per_issue: float = 4.0
    junior_hours_per_review: float = 1.0
    # Devin ACU list price used for cost attribution when the API reports ACUs
    devin_acu_usd: float = 2.25


@lru_cache
def get_settings() -> Settings:
    return Settings()
