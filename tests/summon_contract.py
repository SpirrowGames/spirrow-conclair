"""The ``active_summon`` contract, as the readers of this server apply it.

``tests/data/summon/active_summon_contract.json`` is a byte-identical copy of
the canonical file in SpirrowGames/spirrow-mindwire (origin/main 660dbac,
4,730 bytes). Change it there first, then copy it here byte for byte; the
sha256 below is pinned so a local edit fails the unit suite.

:func:`classify` restates the contract's ``_contract`` text (the same rules as
mindwire's ``parse_active_summon_value``) so this repository can judge its own
GET responses. It is itself checked against every fixture case before it is
trusted to judge anything (``tests/unit/test_active_summon_contract.py``).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "data" / "summon" / "active_summon_contract.json"
FIXTURE_SIZE = 4730
FIXTURE_SHA256 = "e522e200d941d18254b9f5defa7e6d5f14479d95158157cf84577735445bf18a"

#: What THIS server promises for ``since``: UTC, ``+00:00``, six fractional
#: digits (msg-1435). Stricter than the contract, which also accepts ``Z`` and
#: 0-6 digits.
SERVER_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")

_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


def load_cases() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_bytes().decode("utf-8"))["cases"]


def parse_since(value: Any) -> datetime | None:
    if not isinstance(value, str) or not _SINCE_RE.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def classify(payload: dict[str, Any]) -> dict[str, Any]:
    """``{"kind": "none" | "malformed"}`` or ``{"kind": "valid", ...}``.

    For ``valid``, ``since`` is returned as a UTC datetime so two spellings of
    the same instant compare equal.
    """
    value = payload.get("active_summon")
    if value is None:
        return {"kind": "none"}
    if not isinstance(value, dict):
        return {"kind": "malformed"}
    thread_id, by = value.get("thread_id"), value.get("by")
    since = parse_since(value.get("since"))
    if not isinstance(thread_id, str) or not thread_id.strip():
        return {"kind": "malformed"}
    if not isinstance(by, str) or not by.strip():
        return {"kind": "malformed"}
    if since is None:
        return {"kind": "malformed"}
    return {"kind": "valid", "thread_id": thread_id, "since": since, "by": by}


def normalize_expect(expect: dict[str, Any]) -> dict[str, Any]:
    if expect["kind"] != "valid":
        return dict(expect)
    return {**expect, "since": parse_since(expect["since"])}
