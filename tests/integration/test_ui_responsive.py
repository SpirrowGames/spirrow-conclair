"""Tests for the phone layout of /ui.

CSS itself isn't worth asserting on, but one thing here is: the stacked
(phone) table layout renders each cell's label from its ``data-label``
attribute, so a column's name lives in two places -- the ``<th>`` and the
cell. Nothing in the browser complains when they drift; the phone view
just starts labelling values wrongly while the desktop view stays right,
which is the kind of bug you only find by holding a phone.

These tests pin the two together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import AsyncClient

PROJECT = "ui-resp-proj"

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "spirrow_conclair" / "templates"

# `<th(?:\s...)?>` and not `<th[^>]*>`: the latter also matches `<thead>`,
# whose capture then runs to the first real `</th>`.
_TH_RE = re.compile(r"<th(?:\s[^>]*)?>(.*?)</th>", re.DOTALL)
_LABEL_RE = re.compile(r'<td[^>]*\bdata-label="([^"]*)"')


def _headers(html: str) -> list[str]:
    return [re.sub(r"\s+", " ", m).strip() for m in _TH_RE.findall(html)]


def _labels(html: str) -> list[str]:
    return _LABEL_RE.findall(html)


async def _open(client: AsyncClient, thread_id: str) -> None:
    r = await client.post(
        f"/v1/projects/{PROJECT}/threads",
        json={
            "thread_id": thread_id,
            "title": "t",
            "owner": "alice",
            "propose_content": "start",
            "tags": ["gate:naysayer"],
        },
    )
    assert r.status_code == 201, r.text


# ---- label / header parity ------------------------------------------------


@pytest.mark.asyncio
async def test_thread_list_labels_match_its_headers(client: AsyncClient) -> None:
    await _open(client, "T-1")

    body = (await client.get(f"/ui/projects/{PROJECT}/threads")).text

    # The first data row's labels, in order, are the column headers.
    assert _labels(body)[:6] == _headers(body)


@pytest.mark.asyncio
async def test_events_labels_match_its_headers(client: AsyncClient) -> None:
    await _open(client, "T-1")

    body = (await client.get(f"/ui/projects/{PROJECT}/events")).text

    assert _labels(body)[:5] == _headers(body)


def test_integrity_labels_match_its_headers() -> None:
    """Source-level: an integrity issue needs a corrupted DB to render."""
    source = (TEMPLATES / "partials" / "integrity_body.html").read_text()

    assert _labels(source) == _headers(source)


# ---- the stacked layout is actually switched on ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        f"/ui/projects/{PROJECT}/threads",
        f"/ui/projects/{PROJECT}/events",
    ],
)
async def test_list_tables_opt_into_stacking(client: AsyncClient, path: str) -> None:
    """Without the class the labels render but nothing reads them."""
    body = (await client.get(path)).text

    assert 'class="table table-stack"' in body


@pytest.mark.asyncio
async def test_empty_state_row_carries_no_label(client: AsyncClient) -> None:
    """The "no threads" row spans every column and names none of them, so it
    must not pick up a label slot in the stacked view."""
    body = (await client.get(f"/ui/projects/{PROJECT}/threads")).text

    assert "no threads match these filters" in body
    assert _labels(body) == []


# ---- stylesheet ------------------------------------------------------------


@pytest.mark.asyncio
async def test_stylesheet_ships_the_phone_layout(client: AsyncClient) -> None:
    css = (await client.get("/static/css/conclair.css")).text

    assert "@media (max-width: 768px)" in css
    assert ".table-stack" in css


@pytest.mark.asyncio
async def test_pages_declare_a_viewport(client: AsyncClient) -> None:
    """Without this the phone renders at ~980px and scales down, which makes
    every media query above pointless."""
    body = (await client.get(f"/ui/projects/{PROJECT}/threads")).text

    assert 'name="viewport"' in body
    assert "width=device-width" in body
