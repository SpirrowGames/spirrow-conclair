"""Application settings (loaded from .env / environment)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import Depends
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

    # When this deployment started recording close sanctions (see
    # `services.close_sanction`). It is a *deployment* fact -- the instant the
    # recorder went live -- which no constant in the source can know, since
    # code is written before it is shipped. A baked-in date would call every
    # close made between then and the actual rollout corrupt.
    #
    # Left unset, no non-owner close is ever reported as corruption: an
    # unrecorded close cannot be told apart from one written before the
    # recorder existed. That is the safe direction (a missed finding, not a
    # fabricated one) but it does leave the strictest bucket disarmed, so the
    # value is echoed in every `/integrity` response rather than hiding in a
    # config file. Setting it is part of deploying this feature.
    #
    # A naive value is read as UTC.
    sanction_recording_since: datetime | None = None


def get_settings() -> Settings:
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]
