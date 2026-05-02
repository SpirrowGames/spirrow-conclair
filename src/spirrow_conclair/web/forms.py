"""Form helpers — convert x-www-form-urlencoded payloads into pydantic schemas.

T15 stub. POST endpoints (open / post / close) land in T17, where this
module will host CSV-to-list parsing and Form(...) → request schema mapping.
"""

from __future__ import annotations


def parse_csv(value: str | None) -> list[str]:
    """Split a comma-separated form value into a clean list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
