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
    """Count every AST ``Call`` that acquires a row lock via SQLAlchemy.

    Two forms count, because both are how ``FOR UPDATE`` reaches the
    wire in SQLAlchemy 2.x:

    - **method form**: ``select(Thread).with_for_update()`` or
      ``query.with_for_update(...)``. In the AST this is a ``Call``
      whose ``func`` is an ``Attribute`` with ``attr='with_for_update'``.
      The canonical query-level lock idiom; a text scan for the phrase
      as a keyword catches zero of these.
    - **keyword form**: ``session.refresh(thread, with_for_update=True)``.
      The row-lock variant of ``AsyncSession.refresh`` (and of anything
      else that accepts it as a keyword). Only ``=True`` counts;
      ``=False`` is a SQLAlchemy no-op.

    A previous form checked only the keyword; a developer could quietly
    add ``select(Thread).with_for_update()`` in a new file and the
    choke-point invariant would silently regress. That regression was
    the exact failure this counter exists to catch, so both forms are
    now first-class.

    Docstrings, exception messages, and other string literals do not
    count -- the AST distinguishes a call from a mention. A syntax
    error re-raises: silently treating unparseable source as "zero
    calls" would let a broken file hide a new lock.
    """
    tree = ast.parse(src)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # method form: ``<anything>.with_for_update(...)``
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_for_update"
        ):
            count += 1
            continue

        # keyword form: ``f(..., with_for_update=True)``
        for kw in node.keywords:
            if kw.arg != "with_for_update":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                count += 1
                # A single call cannot lock twice; break so a duplicate kw
                # (syntactically odd but legal expression) is not
                # double-counted per call.
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


def test_ast_counter_finds_method_call_form() -> None:
    """``select(Thread).with_for_update()`` also acquires a row lock.

    This is the canonical SQLAlchemy 2.x query-level lock idiom. An
    earlier version of the counter checked only for ``with_for_update``
    as a keyword argument and silently ignored the method call, so a
    developer could quietly add ``select(Thread).with_for_update()`` in
    a new file and the choke-point invariant would regress with no test
    firing. This test is the regression pin for that gap.

    Variants covered here: chained on a ``select(...)``, chained on an
    identifier (as a fluent query builder), and with keyword arguments
    (``nowait=True`` / ``read=True``) -- all of them lock a row.
    """
    method_form = """
from sqlalchemy import select

def a(session):
    return session.execute(select(Thread).with_for_update()).scalar_one()

def b(query):
    return query.with_for_update(nowait=True).all()

def c(query):
    return query.with_for_update(read=True).all()
"""
    assert _count_row_lock_calls(method_form) == 3


def test_ast_counter_counts_mixed_forms_together() -> None:
    """Method-call form and keyword-form both contribute to the same total.

    If a real production file combined the two (e.g. a helper using
    ``.with_for_update()`` and the main choke point using
    ``refresh(..., with_for_update=True)``), the site test needs to see
    both, not one or the other. Pins the union semantics of the counter
    so a future refactor cannot make the counter double-count nor drop
    a form.
    """
    mixed = """
from sqlalchemy import select

def a(session, thread):
    session.execute(select(Thread).with_for_update()).scalar_one()
    session.refresh(thread, with_for_update=True)
"""
    assert _count_row_lock_calls(mixed) == 2
