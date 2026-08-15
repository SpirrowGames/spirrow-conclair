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


async def test_the_activity_key_survives_the_whole_message_lifecycle(
    client: AsyncClient,
) -> None:
    """The one denormalised value in the schema, checked against its source.

    `threads.last_msg_num` is what both triage surfaces rank on. It is
    maintained by exactly two write sites (open_thread and
    post_message_in_session), and a thread's life runs through both plus the
    close path -- so this drives all of them and then asks the audit whether
    the cached rank still equals the messages it summarises.
    """
    await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": "T-live",
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
        },
    )
    # A sibling thread, so the project-wide msg sequence interleaves: a key
    # taken from the project's max rather than the thread's would pass a
    # single-thread test and fail here.
    await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": "T-other",
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
        },
    )
    for _ in range(3):
        r = await client.post(
            "/v1/projects/p/threads/T-live/messages",
            json={"type": "report", "author": "bob", "content": "x"},
        )
        assert r.status_code == 201, r.text
        r = await client.post(
            "/v1/projects/p/threads/T-other/messages",
            json={"type": "report", "author": "bob", "content": "x"},
        )
        assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/projects/p/threads/T-live/close",
        json={"summary_content": "## Resolution\ndone", "author": "alice"},
    )
    assert r.status_code == 201, r.text

    r = await client.get("/v1/projects/p/integrity")
    assert r.status_code == 200
    assert r.json()["issues"] == []

    # And the ranking the listing reads agrees with the msg ids it shows.
    items = (await client.get("/v1/projects/p/threads")).json()["items"]
    by_id = {i["thread_id"]: i for i in items}
    assert by_id["T-live"]["last_msg_id"] > by_id["T-other"]["last_msg_id"]
    assert [i["thread_id"] for i in items] == ["T-live", "T-other"]


async def test_detects_a_stale_activity_key(
    client: AsyncClient,
    session_factory: async_sessionmaker,
) -> None:
    """The check has to be able to fail, or it is decoration.

    Denormalising the sort key bought exactly one new failure mode: the
    cached rank drifting from the msgs. A direct UPDATE is the only way to
    produce it (the write path cannot), and the audit must name it.
    """
    await client.post(
        "/v1/projects/p/threads",
        json={
            "thread_id": "T-1",
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
        },
    )
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE threads SET last_msg_num = 999 "
                "WHERE project = 'p' AND thread_id = 'T-1'"
            )
        )
        await session.commit()

    body = (await client.get("/v1/projects/p/integrity")).json()
    stale = [i for i in body["issues"] if i["type"] == "stale_activity_key"]
    assert len(stale) == 1, body["issues"]
    assert stale[0]["thread_id"] == "T-1"
    assert "msg-001" in stale[0]["details"]
