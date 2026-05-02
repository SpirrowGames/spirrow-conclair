"""UI smoke tests — landing page renders and static assets are served."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_landing_renders(client: AsyncClient) -> None:
    resp = await client.get("/ui/")
    assert resp.status_code == 200
    body = resp.text.lower()
    assert "conclair" in body
    # base.html boilerplate sanity checks
    assert "htmx" in body
    assert "/static/css/conclair.css" in body
    assert "/static/js/conclair.js" in body


@pytest.mark.asyncio
async def test_landing_has_recent_projects_anchor(client: AsyncClient) -> None:
    resp = await client.get("/ui/")
    assert resp.status_code == 200
    # JS bootstraps the recent-projects list into this <ul>; presence in the
    # template is enough for the smoke test.
    assert 'id="recent-projects"' in resp.text


@pytest.mark.asyncio
async def test_static_css_served(client: AsyncClient) -> None:
    resp = await client.get("/static/css/conclair.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "--bg" in resp.text


@pytest.mark.asyncio
async def test_static_js_served(client: AsyncClient) -> None:
    resp = await client.get("/static/js/conclair.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "conclairOpenProject" in resp.text
