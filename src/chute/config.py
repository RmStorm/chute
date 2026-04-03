from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHUTE_", env_file=".env", extra="ignore")

    app_name: str = "chute"
    env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    database_path: Path = Path(".data/chute.sqlite3")
    reconcile_interval_seconds: float = 10.0
    bootstrap_on_startup: bool = True

    github_webhook_secret: str | None = None
    github_app_id: str | None = None
    github_installation_id: str | None = None
    github_private_key_path: Path | None = None
    github_owner: str = Field(default="example")
    github_repo: str = Field(default="monorepo")
    github_api_url: str = "https://api.github.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
