"""Pin the row-lock's single choke point.

The double-close race fix rests on one line -- a
``session.refresh(thread, with_for_update=True)`` at the top of
``post_message_in_session`` -- and the guarantee that this line is the
*only* place per-thread row locking is done. Every write path that adds
a msg to an existing thread funnels through that function, so one line
covers every route.

If a second caller elsewhere in the tree learns to take its own
``with_for_update`` lock on ``threads``, the coverage stops being a
property one file states and starts being a property many files must
independently keep true. That is exactly the kind of drift the audit
sanction recorder's docstring names as the reason the recorder lives at
one call site rather than at each of its callers. This test refuses the
same drift for row locks.

Scope note: the check is over ``src/spirrow_conclair`` only. Third-party
packages and the venv can and do carry the phrase; that is not this
codebase's problem to gate on.
"""

from __future__ import annotations

import re
from pathlib import Path

import spirrow_conclair

_SRC_ROOT = Path(spirrow_conclair.__file__).resolve().parent
# The call form -- not the bare token, which appears in explanatory
# comments too. Counting comment mentions would either force the docstring
# to avoid the very word it needs to name (fragile) or turn the test into a
# lint (noisy). The call form is what actually locks a row.
_CALL_RE = re.compile(r"\bwith_for_update\s*=\s*True\b")


def _call_sites() -> list[tuple[Path, int]]:
    """Every file under src/spirrow_conclair where the row-lock call appears.

    Only executable lines count: a Python line whose first non-whitespace
    character is ``#`` is a comment, and docstrings around the actual call
    need to be free to name the phrase they explain.
    """
    hits: list[tuple[Path, int]] = []
    for path in _SRC_ROOT.rglob("*.py"):
        n = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            n += len(_CALL_RE.findall(line))
        if n:
            hits.append((path, n))
    return hits


def test_with_for_update_lives_at_exactly_one_call_site() -> None:
    """Exactly one file, exactly one call -- inside ``post_message_in_session``.

    A second ``session.refresh(..., with_for_update=True)`` anywhere in the
    tree would fail this test on purpose: the property being pinned is
    "there is one row-lock call site", and one file listing two locks is one
    call too many for a reviewer answering "where does the guarantee live?".
    Comment mentions of the token are not counted -- the docstring at the
    call site needs the phrase to explain itself.
    """
    hits = _call_sites()

    assert len(hits) == 1, (
        f"Expected ``with_for_update=True`` to be called in exactly one source "
        f"file (``api/messages.py``); found it in {[str(p) for p, _ in hits]}. "
        f"If a new lock is genuinely needed, delete this test and replace it "
        f"with one that pins the new invariant explicitly -- do not silently "
        f"loosen the count."
    )
    path, count = hits[0]
    assert path.name == "messages.py" and path.parent.name == "api", (
        f"Expected the row lock to live in ``api/messages.py``; found it in "
        f"{path}. If the choke point has genuinely moved, update this test "
        f"together with the move."
    )
    assert count == 1, (
        f"Expected exactly one ``with_for_update=True`` call in {path.name}, "
        f"found {count}. Two locks at the same site is one lock too many for "
        f"a reader answering 'where is the guarantee?'."
    )
