"""End-to-end tests for the integrity audit endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


async def test_clean_project_has_no_issues(client: AsyncClient) -> None:
    await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": "T-1",
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
        },
    )
    r = await client.get("/v1/projects/p/integrity")
    assert r.status_code == 200
    body = r.json()
    assert body["issues"] == []
    assert body["issue_count"] == 0
    assert "checked_at" in body


async def test_detects_missing_propose(
    client: AsyncClient,
    session_factory: async_sessionmaker,
) -> None:
    """Inject a thread without any messages (would normally be rejected by
    the API, but a manual INSERT bypasses the asserts) and audit picks it up.
    """
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO threads "
                "(project, thread_id, title, owner, status, "
                " created_at, created_by_msg, affects_threads, tags) "
                "VALUES ('p','T-orphan','t','alice','active', "
                ":now, 'msg-x', '[]', '[]')"
            ),
            {"now": datetime.now(timezone.utc)},
        )
        await session.commit()

    r = await client.get("/v1/projects/p/integrity")
    body = r.json()
    types = {i["type"] for i in body["issues"]}
    assert "missing_propose" in types


async def test_detects_inconsistent_resolved(
    client: AsyncClient,
    session_factory: async_sessionmaker,
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO threads "
                "(project, thread_id, title, owner, status, "
                " created_at, created_by_msg, resolved_by_msg, "
                " affects_threads, tags) "
                "VALUES ('p','T-bad-resolved','t','alice','resolved', "
                ":now, 'msg-x', NULL, '[]', '[]')"
            ),
            {"now": datetime.now(timezone.utc)},
        )
        await session.commit()

    r = await client.get("/v1/projects/p/integrity")
    issues = r.json()["issues"]
    inconsistent = [i for i in issues if i["type"] == "inconsistent_resolved"]
    assert any(i["thread_id"] == "T-bad-resolved" for i in inconsistent)


async def test_audit_is_project_scoped(
    client: AsyncClient,
    session_factory: async_sessionmaker,
) -> None:
    """Issues in project A must not appear in project B's audit."""
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO threads "
                "(project, thread_id, title, owner, status, "
                " created_at, created_by_msg, affects_threads, tags) "
                "VALUES ('A','T-orphan','t','alice','active', "
                ":now, 'msg-x', '[]', '[]')"
            ),
            {"now": datetime.now(timezone.utc)},
        )
        await session.commit()

    r = await client.get("/v1/projects/B/integrity")
    assert r.json()["issues"] == []
    r = await client.get("/v1/projects/A/integrity")
    assert any(i["thread_id"] == "T-orphan" for i in r.json()["issues"])
