"""Async SQLAlchemy engine / session helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from spirrow_conclair.config import Settings, get_settings


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_db(settings: Settings) -> None:
    global _engine, _sessionmaker
    _engine = make_engine(settings)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("DB not initialized; init_db() must run during app startup")
    async with _sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def health_check() -> bool:
    """Lightweight DB ping for /health."""
    if _engine is None:
        return False
    from sqlalchemy import text

    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _settings_for_alembic() -> Settings:
    """Used by alembic env.py to read the same DATABASE_URL the app uses."""
    return get_settings()
