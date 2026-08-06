"""The success flash has to go away on its own.

A flash partial is swapped into #flash-post / #flash-open / #flash-close
and nothing removed it, so a green "posted msg-014 (report)" stayed on
the page until the next post or a reload. On a phone, where the flash and
the next form are both on screen, it reads as the status of whatever you
did after it.

Dismissal is client-side, so these tests assert the two halves ship and
that they agree with each other -- the class the script adds has to be
the class the stylesheet fades, and neither file fails visibly if that
drifts.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

PROJECT = "flash-test-proj"


@pytest.mark.asyncio
async def test_success_flash_is_marked_as_a_success(client: AsyncClient) -> None:
    """The class the dismissal keys on is the one a success actually gets."""
    resp = await client.post(
        f"/ui/projects/{PROJECT}/threads",
        data={
            "thread_id": "T-FLASH-1",
            "title": "flash",
            "owner": "flash-tester",
            "propose_content": "body",
        },
    )

    assert resp.status_code == 200
    assert "alert-success" in resp.text


@pytest.mark.asyncio
async def test_error_flash_is_not_marked_as_a_success(client: AsyncClient) -> None:
    """Errors must not be swept up by the dismissal.

    A failure that erases itself after six seconds is worse than one that
    stays: the post did not happen, and the page otherwise looks the same
    either way.
    """
    payload = {
        "thread_id": "T-FLASH-DUP",
        "title": "first",
        "owner": "flash-tester",
        "propose_content": "first",
    }
    await client.post(f"/ui/projects/{PROJECT}/threads", data=payload)
    resp = await client.post(f"/ui/projects/{PROJECT}/threads", data=payload)

    assert resp.status_code == 200
    assert "alert-error" in resp.text
    assert "alert-success" not in resp.text


@pytest.mark.asyncio
async def test_script_dismisses_success_flashes_only(client: AsyncClient) -> None:
    js = (await client.get("/static/js/conclair.js")).text

    assert "htmx:afterSwap" in js
    assert ".alert-success" in js
    # The error class must not appear in the dismissal path at all.
    assert ".alert-error" not in js


@pytest.mark.asyncio
async def test_the_fade_class_is_styled(client: AsyncClient) -> None:
    """The script adds `alert-leaving` and the stylesheet has to know it.

    If it does not, the banner does not fade -- it simply vanishes four
    tenths of a second later than it would have. That is a difference no
    test would catch except this one, and nobody would think to look for
    it in the other file.
    """
    js = (await client.get("/static/js/conclair.js")).text
    css = (await client.get("/static/css/conclair.css")).text

    assert "alert-leaving" in js
    assert ".alert-leaving" in css
    assert "transition" in css.split(".alert-success")[1].split("}")[0]
