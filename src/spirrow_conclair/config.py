"""Application settings (loaded from .env / environment)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        ...,
        description=(
            "Async SQLAlchemy URL, e.g. "
            "postgresql+asyncpg://conclair_app:***@127.0.0.1:5432/conclair"
        ),
    )
    port: int = 8115
    log_level: str = "INFO"

    # Connection pool sizing for SQLAlchemy async engine.
    db_pool_size: int = 5
    db_max_overflow: int = 10


def get_settings() -> Settings:
    return Settings()
