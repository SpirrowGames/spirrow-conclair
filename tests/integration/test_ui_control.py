"""UI tests for the loop control widget."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PROJECT = "ui-ctl-proj"


async def _widget(client: AsyncClient, project: str = PROJECT) -> str:
    resp = await client.get(f"/ui/projects/{project}/control/_widget")
    assert resp.status_code == 200, resp.text
    return resp.text


async def _set(client: AsyncClient, project: str = PROJECT, **form) -> tuple[int, str]:
    resp = await client.post(f"/ui/projects/{project}/control", data=form)
    return resp.status_code, resp.text


# ---- rendering ------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_list_page_embeds_the_widget(client: AsyncClient) -> None:
    """First paint, not a post-load fetch — an operator arriving to stop
    something sees the state immediately."""
    resp = await client.get(f"/ui/projects/{PROJECT}/threads")

    assert resp.status_code == 200
    assert 'id="control-widget"' in resp.text
    assert "RUN" in resp.text
    assert "SUPERVISED" in resp.text
    assert "HOLD" in resp.text


@pytest.mark.asyncio
async def test_widget_polls_itself(client: AsyncClient) -> None:
    body = await _widget(client)
    assert 'hx-trigger="every 7s"' in body
    assert f'hx-get="/ui/projects/{PROJECT}/control/_widget"' in body


@pytest.mark.asyncio
async def test_unconfigured_widget_shows_default_and_says_so(
    client: AsyncClient,
) -> None:
    body = await _widget(client, "never-touched-ui")

    assert "control-run" in body
    assert "未設定" in body
    # Nothing to be pending about: the project is running on the default.
    assert "反映待ち" not in body


@pytest.mark.asyncio
async def test_widget_states_the_actor_is_not_authentication(
    client: AsyncClient,
) -> None:
    """P-3: the tailnet is the trust boundary. The UI has to say that the
    name attached to a change is a record, not a credential."""
    body = await _widget(client)
    assert "認証ではありません" in body


# ---- setting --------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_sets_state_and_returns_the_widget(client: AsyncClient) -> None:
    code, body = await _set(client, state="hold", author="takahito")

    assert code == 200, body
    assert 'id="control-widget"' in body
    assert "control-hold" in body

    # It latched — the /v1 read agrees.
    state = (await client.get(f"/v1/projects/{PROJECT}/control")).json()
    assert state["desired_state"] == "hold"
    assert state["desired_actor"] == "takahito"


@pytest.mark.asyncio
async def test_blank_author_is_refused_but_keeps_the_widget(
    client: AsyncClient,
) -> None:
    """The error renders inside the widget. Swapping a flash partial over
    it would remove the buttons and the poll trigger, leaving no way to
    retry from this screen."""
    code, body = await _set(client, state="hold", author="")

    assert code == 200, body
    assert 'id="control-widget"' in body
    assert "author を入れてください" in body

    state = (await client.get(f"/v1/projects/{PROJECT}/control")).json()
    assert state["configured"] is False


@pytest.mark.asyncio
async def test_unknown_state_is_refused(client: AsyncClient) -> None:
    code, body = await _set(client, state="paused", author="takahito")

    assert code == 200, body
    assert "未知の state" in body

    state = (await client.get(f"/v1/projects/{PROJECT}/control")).json()
    assert state["configured"] is False


# ---- desired vs observed --------------------------------------------------


@pytest.mark.asyncio
async def test_widget_shows_pending_while_loop_has_not_caught_up(
    client: AsyncClient,
) -> None:
    await client.post(
        f"/v1/projects/{PROJECT}/control/observed",
        json={"state": "run", "actor": "mindwire-conductor"},
    )
    await _set(client, state="hold", author="takahito")

    body = await _widget(client)

    assert "反映待ち" in body
    assert "mindwire-conductor" in body


@pytest.mark.asyncio
async def test_widget_drops_pending_once_the_loop_agrees(
    client: AsyncClient,
) -> None:
    await _set(client, state="hold", author="takahito")
    await client.post(
        f"/v1/projects/{PROJECT}/control/observed",
        json={"state": "hold", "actor": "mindwire-conductor"},
    )

    body = await _widget(client)

    assert "反映待ち" not in body


@pytest.mark.asyncio
async def test_stale_observed_is_flagged_as_a_guess(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The staleness line is a presumption, not a diagnosis — a loop
    mid-way through a long implementation turn looks identical."""
    await client.post(
        f"/v1/projects/{PROJECT}/control/observed",
        json={"state": "run", "actor": "mindwire-conductor"},
    )
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    await db_session.execute(
        text(
            "UPDATE project_control SET observed_at = :t WHERE project = :p"
        ),
        {"t": old, "p": PROJECT},
    )
    await db_session.commit()

    body = await _widget(client)

    assert "ループが動いていない可能性があります" in body
    assert "これは推測です" in body


@pytest.mark.asyncio
async def test_fresh_observed_is_not_flagged(client: AsyncClient) -> None:
    await client.post(
        f"/v1/projects/{PROJECT}/control/observed",
        json={"state": "run", "actor": "mindwire-conductor"},
    )

    body = await _widget(client)

    assert "ループが動いていない可能性があります" not in body


# ---- history --------------------------------------------------------------


@pytest.mark.asyncio
async def test_widget_lists_recent_history(client: AsyncClient) -> None:
    await _set(client, state="hold", author="takahito")
    await _set(client, state="run", author="bohr-operator")

    body = await _widget(client)

    assert "takahito" in body
    assert "bohr-operator" in body
