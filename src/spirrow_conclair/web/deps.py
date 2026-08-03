"""Shared UI dependencies: Jinja2Templates singleton + filters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi.templating import Jinja2Templates

_templates: Jinja2Templates | None = None


def set_templates(t: Jinja2Templates) -> None:
    global _templates
    _templates = t
    t.env.filters["iso"] = _iso_filter
    t.env.filters["ago"] = _ago_filter


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


def _ago_filter(value: Any) -> str:
    """Coarse "how long ago" for the loop-control widget.

    Deliberately vague: the widget's job is to say whether the loop read
    the state recently, not to time it. A future timestamp (clock skew
    between this host and the one running the loop) is reported as
    "たった今" rather than a negative age.
    """
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - value).total_seconds()
    if seconds < 60:
        return "たった今"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 時間前"
    return f"{hours // 24} 日前"
