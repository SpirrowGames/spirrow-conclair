"""Integration test fixtures.

Spins up a throw-away postgres:16 container per test session, runs
alembic migrations against it once, and cleans the chatroom tables
between tests so each test sees an empty DB.

The `client` fixture wires an httpx.AsyncClient onto the in-process
FastAPI app via ASGITransport, so test HTTP calls don't go over a real
socket. We manage init_db / dispose_db manually because ASGITransport
does not deliver ASGI lifespan events to the app.

Engine fixtures are function-scoped: pytest-asyncio (in auto mode) gives
each test its own event loop by default, and asyncpg connections cannot
cross loop boundaries. Re-creating the engine per test is the simplest
way to avoid "attached to a different loop" errors.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

# ---------------------------------------------------------------------------
# Session scope: one postgres container, run migrations once.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer(
        "postgres:16",
        username="conclair_app",
        password="conclair_test",
        dbname="conclair",
        driver="asyncpg",
    )
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
def _run_migrations(database_url: str) -> Iterator[None]:
    """Apply alembic head once at session start.

    Sync entry point — alembic's env.py calls asyncio.run() internally,
    which conflicts with pytest-asyncio's running loop inside async
    tests. Doing the migration here at session scope keeps it out of the
    test loops.
    """
    os.environ["DATABASE_URL"] = database_url
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")
    yield


# ---------------------------------------------------------------------------
# Function scope: clean tables, fresh app + httpx client.
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE actor_read_cursors, chatroom_events, messages, "
                "threads RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncClient]:
    os.environ["DATABASE_URL"] = database_url
    from spirrow_conclair.config import get_settings
    from spirrow_conclair.db import dispose_db, init_db
    from spirrow_conclair.main import create_app

    settings = get_settings()
    init_db(settings)
    app = create_app(settings)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as c:
            yield c
    finally:
        await dispose_db()
