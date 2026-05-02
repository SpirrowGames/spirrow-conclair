"""UI router package — Jinja2 + HTMX human-facing views.

Loopback-only chatroom browser/participant UI. Routes live under /ui/...
The /v1/... JSON API is unaffected.
"""

from __future__ import annotations

from spirrow_conclair.web.routes import router

__all__ = ["router"]
