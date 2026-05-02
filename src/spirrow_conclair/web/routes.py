"""UI routes (Jinja2 + HTMX). Mounted under /ui in main.py.

T15 scope: landing page only. Thread / event / integrity views and
form-post endpoints follow in T16 / T17.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from spirrow_conclair import __version__
from spirrow_conclair.web.deps import get_templates

router = APIRouter(prefix="/ui", tags=["ui"])


@router.get("/", response_class=HTMLResponse, summary="Landing page (project picker)")
async def landing(request: Request) -> HTMLResponse:
    return get_templates().TemplateResponse(
        request,
        "landing.html",
        {"active_page": "landing", "version": __version__},
    )
