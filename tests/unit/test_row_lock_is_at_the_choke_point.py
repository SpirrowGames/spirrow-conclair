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

The count is over actual function-call sites, walked from the parsed
AST -- not text occurrences. A previous form used a text scan that
skipped ``#`` comment lines but still counted the phrase when it
appeared inside a Python string (docstring, exception message, wrapper
attribute), which contradicted its own prose and let the same file
mention the phrase in explanation while claiming the count was one.
Going through ``ast`` treats a mention *as* a mention and a call *as* a
call; that is the distinction the invariant actually cares about.

Scope note: the check is over ``src/spirrow_conclair`` only. Third-party
packages and the venv can and do carry the phrase; that is not this
codebase's problem to gate on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import spirrow_conclair

_SRC_ROOT = Path(spirrow_conclair.__file__).resolve().parent


def _count_row_lock_calls(src: str) -> int:
    """Count ``Call`` nodes that pass ``with_for_update=True`` as a keyword.

    The keyword is what a SQLAlchemy ``session.refresh`` (or a
    ``select(...).with_for_update()`` variant that takes it as a kw) does
    to acquire a row lock. Any call site that lands the phrase as a real
    argument counts; docstrings, comments, exception strings, and
    identifier references (e.g. a wrapper attribute called
    ``with_for_update``) do not.

    A syntax error inside ``src`` re-raises: the fix's guarantee cannot
    be pinned against source the parser cannot read, and silently
    treating unparseable files as "zero calls" would let a broken file
    hide a new lock.
    """
    tree = ast.parse(src)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "with_for_update":
                continue
            # Only ``with_for_update=True`` counts as a lock acquisition;
            # ``with_for_update=False`` (or any non-True literal) is a
            # SQLAlchemy no-op for our purposes.
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                count += 1
                # Same call cannot both take the lock and not; break so a
                # duplicate kw (a syntactically odd but legal expression)
                # is not double-counted per call.
                break
    return count


def _call_sites() -> list[tuple[Path, int]]:
    """Every source file under ``src/spirrow_conclair`` whose AST holds a
    ``with_for_update=True`` call, with its per-file call count.
    """
    hits: list[tuple[Path, int]] = []
    for path in _SRC_ROOT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        n = _count_row_lock_calls(src)
        if n:
            hits.append((path, n))
    return hits


def test_with_for_update_lives_at_exactly_one_call_site() -> None:
    """Exactly one file, exactly one call -- inside ``post_message_in_session``.

    A second ``session.refresh(..., with_for_update=True)`` anywhere in the
    tree would fail this test on purpose: the property being pinned is
    "there is one row-lock call site", and one file listing two locks is one
    call too many for a reviewer answering "where does the guarantee live?".
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


def test_ast_counter_ignores_the_phrase_in_docstrings_and_strings() -> None:
    """The counter must not fire on a mere textual mention.

    Fixture strings hold the phrase in every position that is not a call:
    module docstring, function docstring, exception argument, and a
    variable assigned a string equal to the argument form. Zero of those
    are row-lock acquisitions; the count must be zero.

    Without this test the previous text-scan form would pass silently on
    a file where a developer explains the lock in the docstring above the
    real call, and no reviewer would know the invariant had regressed.
    """
    only_docstrings = '''
"""with_for_update=True appears in this module docstring."""

def f():
    """with_for_update=True is described here too."""
    raise ValueError("with_for_update=True is not a real call in a message")

banner = "with_for_update=True"  # a string literal, not an argument
'''
    assert _count_row_lock_calls(only_docstrings) == 0


def test_ast_counter_finds_the_call_when_present() -> None:
    """Positive control: a real ``session.refresh(..., with_for_update=True)``
    is counted.

    Belt-and-braces with the site test above: if the ast helper stopped
    counting anything at all, the site test would still pass on a diff
    that quietly deleted the call. This test does not depend on the
    production tree, so it catches the counter's regression on its own.
    """
    with_a_call = """
async def h(session, thread):
    await session.refresh(thread, with_for_update=True)
"""
    assert _count_row_lock_calls(with_a_call) == 1


def test_ast_counter_ignores_with_for_update_false() -> None:
    """``with_for_update=False`` does not take the lock (SQLAlchemy no-op),
    so it must not count against the choke-point invariant either.
    """
    with_a_falsy_call = """
async def h(session, thread):
    await session.refresh(thread, with_for_update=False)
"""
    assert _count_row_lock_calls(with_a_falsy_call) == 0
