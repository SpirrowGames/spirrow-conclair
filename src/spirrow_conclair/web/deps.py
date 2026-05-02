"""Shared UI dependencies: Jinja2Templates singleton + filters."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi.templating import Jinja2Templates

_templates: Jinja2Templates | None = None


def set_templates(t: Jinja2Templates) -> None:
    global _templates
    _templates = t
    t.env.filters["iso"] = _iso_filter


def get_templates() -> Jinja2Templates:
    if _templates is None:
        raise RuntimeError("Templates not initialized; main.py must call set_templates()")
    return _templates


def _iso_filter(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)
