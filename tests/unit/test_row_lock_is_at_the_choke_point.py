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

Two asserts, one scan, opposite directions
------------------------------------------

One walk of the AST produces one set of call sites, and two independent
tests read it:

- **A** -- ``test_with_for_update_lives_at_exactly_one_call_site``: the set
  has exactly one member, in ``api/messages.py``. Over-approximate on
  purpose: anything the parser cannot *prove* is a no-op counts, so a
  second lock cannot hide behind an expression.
- **B** -- ``test_the_one_row_lock_call_asks_for_it_unconditionally``: that
  member's value is the literal ``True``. Strict on purpose: anything the
  parser cannot *prove* is the lock we mean is refused.

They want opposite error directions, which is why they cannot be one
cleverer check. Narrowing the scan to literal ``True`` would satisfy B and
blind A -- a second site spelled ``with_for_update=some_flag`` would stop
being counted and the total would stay at one. Widening it to satisfy A
alone is what left the hole B now fills. The argument is msg-482 §3.

That it worked out that way was measured, not reasoned: before shipping,
each value form below was substituted into the real call site and each
turned B red while A stayed green, and a second site spelled
``with_for_update=some_flag`` in a new file turned A red -- which is how
we know adding B did not blind A. The full matrix is in the message of
the commit that added this test.

What is pinned, and what is merely assumed
------------------------------------------

======  ====================================================  ============
level   property                                              pinned by
======  ====================================================  ============
P1      row locking happens at exactly one call site          A (here)
P2      the deciding read is a *reload* of committed state,   the barrier
        not the caller's stale instance                       tests
P3      writes to one thread queue; writes to different       the
        threads do not                                        serialisation
                                                              tests
P4'     that reload asks for ``FOR UPDATE`` unconditionally   B (here)
P4"     the request is honoured and the second reader         *nothing we*
        actually blocks                                       *own*
======  ====================================================  ============

P4" is Postgres's behaviour and SQLAlchemy's, not ours. It is assumed, and
saying so is the point of listing it: an earlier draft of this work set out
to test it with a wall-clock seam and was talked out of it (msg-481,
msg-482 §1) on the grounds that a test whose oracle is a clock is a test
that goes green for the wrong reason under CI load. P3's serialisation
tests already carry that weakness and admit it in their own docstring; a
second such test was not worth its upkeep. So the honest claim this file
makes is P1 and P4', not P4.

Scope note: the check is over ``src/spirrow_conclair`` only. Third-party
packages and the venv can and do carry the phrase; that is not this
codebase's problem to gate on.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import spirrow_conclair

_SRC_ROOT = Path(spirrow_conclair.__file__).resolve().parent


@dataclass(frozen=True)
class _RowLockSite:
    """One AST call site that acquires a per-thread row lock.

    Carries the node, not just a tally, because this test's failure string
    *is* its primary artefact -- on a green run it says nothing at all. The
    probe run recorded in msg-460 §1.1 failed with ``found it in []``, which
    told the reader neither which form had changed nor where, and the
    afternoon went into rediscovering that by hand. ``describe()`` exists so
    a failure reads ``messages.py:121: with_for_update=Name('some_flag')``.

    ``value`` is the keyword's value node for the keyword form, and ``None``
    for the method-call form (``select(Thread).with_for_update()``), which
    has no such node.
    """

    lineno: int
    value: ast.expr | None

    def describe(self, path: Path | None = None) -> str:
        where = f"{path.name}:{self.lineno}" if path is not None else f"line {self.lineno}"
        if self.value is None:
            return f"{where}: method-call form ``.with_for_update(...)``"
        return f"{where}: with_for_update={type(self.value).__name__}({ast.unparse(self.value)!r})"


def _is_literal_true(value: ast.expr | None) -> bool:
    """Is this node the literal ``True`` and nothing else?

    Compared with ``is``, never ``==``: ``1 == True`` is True in Python, so
    an equality check would wave ``with_for_update=1`` through. ``is`` also
    makes an ``isinstance(..., bool)`` guard redundant -- only the ``True``
    singleton itself passes.

    Everything that is not an ``ast.Constant`` fails here by design, and the
    design is the point: ``flag if c else False`` (``IfExp``), ``bool(flag)``
    (``Call``) and ``LOCK_ROWS`` (``Name``) are all rejected without the
    checker trying to reason about what they evaluate to. Do not "improve"
    this with constant folding or import chasing. A checker that decides
    some dynamic expressions are fine has re-opened the hole it was added to
    close, and the reason it cannot win that argument is written out in
    msg-460 §4: a static check cannot be asked to over-approximate and
    under-approximate at the same time. That is why there are two asserts
    here rather than one cleverer one.
    """
    return isinstance(value, ast.Constant) and value.value is True


def _collect_row_lock_sites(src: str) -> list[_RowLockSite]:
    """Every AST ``Call`` that acquires a row lock via SQLAlchemy.

    Two forms count, because both are how ``FOR UPDATE`` reaches the
    wire in SQLAlchemy 2.x:

    - **method form**: ``select(Thread).with_for_update()`` or
      ``query.with_for_update(...)``. In the AST this is a ``Call``
      whose ``func`` is an ``Attribute`` with ``attr='with_for_update'``.
      The canonical query-level lock idiom; a text scan for the phrase
      as a keyword catches zero of these.
    - **keyword form**: ``session.refresh(thread, with_for_update=X)``.
      The row-lock variant of ``AsyncSession.refresh`` (and of anything
      else that accepts it as a keyword). Counted for every ``X`` **except**
      the two literal values SQLAlchemy treats as "do not lock":
      ``ast.Constant`` with value ``False`` and ``ast.Constant`` with value
      ``None``. Everything else -- ``=True``, a dict of options
      (``{"nowait": True}``), a variable name, a function call, any
      expression the parser cannot statically prove is one of those two
      no-op literals -- counts as a lock acquisition.

    The rule for the keyword branch is deliberately **negate the known-safe
    set**, not "match the known-lock set". Bohr's msg-455 §9.A: a false
    positive costs a developer one moment of thought, but a false negative
    compromises the invariant entirely and silently. Any other cost split
    would allow a canonical SQLAlchemy call to slip past the counter --
    ``with_for_update={"nowait": True}`` and ``with_for_update=some_flag``
    are both proper lock idioms and neither would be caught by "must be
    literal True".

    A previous form checked only the literal-True keyword; a developer
    could quietly add ``select(Thread).with_for_update()`` in a new file,
    or pass a dict of options, and the choke-point invariant would
    silently regress. Both regressions are what this counter exists to
    catch, so both forms and every non-no-op keyword value are now
    first-class.

    Docstrings, exception messages, and other string literals do not
    count -- the AST distinguishes a call from a mention. A syntax
    error re-raises: silently treating unparseable source as "zero
    calls" would let a broken file hide a new lock.

    **Membership is deliberately unchanged** by the strict check added
    alongside it. It is tempting to read "the lock value must be literal
    ``True``" as an instruction to narrow *this* set to literal ``True``,
    and that would be a regression: a second site written as
    ``with_for_update=some_flag`` would then stop being counted, the total
    would stay at one, and the two-sites-now check would go quietly blind
    -- the exact false negative msg-455 §9.A reversed the rule to prevent.
    The two questions want opposite error directions, so they get two
    asserts over one scan (msg-482 §3), not one scan tuned two ways.
    """
    tree = ast.parse(src)
    sites: list[_RowLockSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # method form: ``<anything>.with_for_update(...)``
        if isinstance(node.func, ast.Attribute) and node.func.attr == "with_for_update":
            sites.append(_RowLockSite(lineno=node.lineno, value=None))
            continue

        # keyword form: ``f(..., with_for_update=X)``. Negate-known-safe:
        # only skip when X is *provably* the SQLAlchemy no-op (literal
        # False or literal None). Every other value counts -- including
        # dynamic expressions the parser cannot decide.
        for kw in node.keywords:
            if kw.arg != "with_for_update":
                continue
            if isinstance(kw.value, ast.Constant) and (
                kw.value.value is False or kw.value.value is None
            ):
                # Known-safe no-op: SQLAlchemy issues no FOR UPDATE for
                # literal False or literal None. Compared with ``is``, not
                # ``in (False, None)``, because ``0 == False`` is True in
                # Python and a stray ``with_for_update=0`` should not be
                # silently exempted -- it is not a known-safe idiom.
                # Do NOT count.
                break
            sites.append(_RowLockSite(lineno=node.lineno, value=kw.value))
            # A single call cannot lock twice; break so a duplicate kw
            # (syntactically odd but legal expression) is not
            # double-counted per call.
            break
    return sites


def _count_row_lock_calls(src: str) -> int:
    """How many row-lock call sites the source holds.

    A thin count over :func:`_collect_row_lock_sites`, kept so the counter's
    membership rule can be exercised on its own with small fixture strings.
    The set is the same one both asserts read; there is no second notion of
    "counts as a lock" anywhere in this file.
    """
    return len(_collect_row_lock_sites(src))


def _call_sites() -> list[tuple[Path, list[_RowLockSite]]]:
    """Every source file under ``src/spirrow_conclair`` whose AST holds a
    row-lock call, with the sites found in it.
    """
    hits: list[tuple[Path, list[_RowLockSite]]] = []
    for path in _SRC_ROOT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        sites = _collect_row_lock_sites(src)
        if sites:
            hits.append((path, sites))
    return hits


def test_with_for_update_lives_at_exactly_one_call_site() -> None:
    """Exactly one file, exactly one call -- inside ``post_message_in_session``.

    A second ``session.refresh(..., with_for_update=True)`` anywhere in the
    tree would fail this test on purpose: the property being pinned is
    "there is one row-lock call site", and one file listing two locks is one
    call too many for a reviewer answering "where does the guarantee live?".
    """
    hits = _call_sites()

    found = [f"{p}: {[s.describe() for s in sites]}" for p, sites in hits]
    assert len(hits) == 1, (
        f"Expected a row lock to be taken in exactly one source file "
        f"(``api/messages.py``); found it in {found}. "
        f"If a new lock is genuinely needed, delete this test and replace it "
        f"with one that pins the new invariant explicitly -- do not silently "
        f"loosen the count."
    )
    path, sites = hits[0]
    assert path.name == "messages.py" and path.parent.name == "api", (
        f"Expected the row lock to live in ``api/messages.py``; found it in "
        f"{path}. If the choke point has genuinely moved, update this test "
        f"together with the move."
    )
    assert len(sites) == 1, (
        f"Expected exactly one row-lock call in {path.name}, found "
        f"{len(sites)}: {[s.describe(path) for s in sites]}. Two locks at the "
        f"same site is one lock too many for a reader answering 'where is the "
        f"guarantee?'."
    )


def test_the_one_row_lock_call_asks_for_it_unconditionally() -> None:
    """The site's ``with_for_update`` value is the literal ``True`` -- P4'.

    Assert B of the pair described in the module docstring. It reads the
    same scan as the site test above and asks the opposite-facing question:
    not "has a second lock appeared?" but "does the one we have still take
    the lock on every call?".

    What it buys, measured against SQLAlchemy 2.0.49 rather than assumed --
    ``ForUpdateArg._from_argument`` decides this, and its own guard is
    ``with_for_update in (None, False)``:

    ==========================  ==========================================
    value                       what actually reaches Postgres
    ==========================  ==========================================
    ``True``                    ``FOR UPDATE``          -- the invariant
    ``{"read": True}``          ``FOR SHARE``           -- **silent**
    ``{"nowait": True}``        ``FOR UPDATE NOWAIT``
    ``{"skip_locked": True}``   ``FOR UPDATE SKIP LOCKED``
    ``0``                       no lock at all          -- **silent**
    ``some_flag`` (falsy)       no lock at all          -- **silent**
    ``1``                       ``TypeError`` at runtime
    ==========================  ==========================================

    ``FOR SHARE`` is the worst of them and the reason this assert is worth
    its line count. Share locks do not block each other, so two concurrent
    closes would both read ``active``, both pass ``assert_thread_writable``,
    and both write a closing msg -- the original defect back in full, with
    every response a 2xx and nothing in any log. ``0`` is quieter still:
    SQLAlchemy compares with ``==``, so ``0 == False`` makes it a no-op,
    while this file's counter compares with ``is`` and so still sees a site
    -- which is precisely the split that lets the count stay at one while
    the lock is gone. Every silent row above is caught here.

    None of that is a claim about Postgres. This asserts only that *our*
    code asks for ``FOR UPDATE`` unconditionally (P4'); that the request is
    then honoured (P4") is an assumption about the database and the driver,
    stated in the module docstring and not tested by anything we own.
    """
    hits = _call_sites()
    assert hits, "No row-lock call site found at all; see the site test above."

    offenders = [
        (path, site) for path, sites in hits for site in sites if not _is_literal_true(site.value)
    ]

    assert not offenders, (
        "The row lock must be requested unconditionally: "
        "``with_for_update=True``, the literal, and nothing else. Found "
        f"{[s.describe(p) for p, s in offenders]}. "
        "A dict of options, a runtime flag, or any expression whose value "
        "this checker cannot see is a different lock or no lock at all -- "
        "``{'read': True}`` compiles to ``FOR SHARE``, which does not block "
        "a second reader and restores the double-close race in full while "
        "every request still returns 2xx. If a weaker lock is genuinely "
        "wanted, say so in this test and in the docstring of "
        "``post_message_in_session`` together -- do not change the call "
        "alone."
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


def test_ast_counter_ignores_with_for_update_none_the_sqlalchemy_default() -> None:
    """``with_for_update=None`` is SQLAlchemy's default and issues no ``FOR
    UPDATE``. It must not count against the choke-point invariant.

    Pinned by name and docstring so a future "help, the counter is
    over-eager" refactor does not silently switch this to "count
    everything, including None": the whole point of the negate-known-safe
    rule is that ``None`` and ``False`` are the *only* two literal values
    the parser is allowed to prove safe. Any other change to this
    predicate needs to explain why the new safe-set is provably
    exhaustive.
    """
    with_a_none_call = """
async def h(session, thread):
    await session.refresh(thread, with_for_update=None)
"""
    assert _count_row_lock_calls(with_a_none_call) == 0


def test_ast_counter_counts_dynamic_variable_form() -> None:
    """``with_for_update=some_flag`` counts even though the parser cannot
    prove which branch fires.

    A developer plumbing a runtime flag into the lock parameter is
    acquiring a row lock **when the flag is truthy**. The counter cannot
    peek at runtime, so it takes the safe side: any value that is not the
    two literal no-op constants counts. Regression pin for msg-455 §2's
    "dynamic ``Name``" case, which the previous literal-True-only counter
    silently ignored.
    """
    dynamic_form = """
async def h(session, thread, flag):
    await session.refresh(thread, with_for_update=flag)
"""
    assert _count_row_lock_calls(dynamic_form) == 1


def test_ast_counter_counts_dict_options_form() -> None:
    """``with_for_update={"nowait": True}`` counts.

    SQLAlchemy accepts a dict of lock options (nowait, skip_locked, of,
    read, key_share) in the same parameter that also takes True/False.
    A dict is a lock acquisition, full stop. Regression pin for the
    speculative-blind-spot advisory flagged on PR #19 (msg-393) and
    graduated to a real defect in the PR #21 gate review (msg-455 §2).
    """
    dict_form = """
async def h(session, thread):
    await session.refresh(thread, with_for_update={"nowait": True})
"""
    assert _count_row_lock_calls(dict_form) == 1


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


def _only_site_value(src: str) -> ast.expr | None:
    """The single collected site's value node, for predicate fixtures."""
    sites = _collect_row_lock_sites(src)
    assert len(sites) == 1, f"fixture should hold exactly one site, got {sites}"
    return sites[0].value


def test_strict_predicate_accepts_only_the_literal_true() -> None:
    """Positive control for assert B: ``with_for_update=True`` passes.

    Paired with the rejection tests below so a refactor that broke the
    predicate open (``return True``) or shut (``return False``) is caught
    from both sides. Without this, "the predicate always says no" would
    look identical to "the code is correct" on a green production tree.
    """
    src = """
async def h(session, thread):
    await session.refresh(thread, with_for_update=True)
"""
    assert _is_literal_true(_only_site_value(src)) is True


def test_strict_predicate_rejects_one_the_true_equals_one_trap() -> None:
    """``with_for_update=1`` is rejected.

    ``1 == True`` in Python, so a predicate written with ``==`` would accept
    this. Measured against SQLAlchemy 2.0.49: ``1`` is neither caught by the
    ``in (None, False)`` no-op guard nor by the ``is True`` branch, so it
    falls through to ``ForUpdateArg(**1)`` and raises ``TypeError``. Loud
    rather than silent -- but a lock that raises is still not a lock, and
    the predicate has no business deciding which failures are acceptable.
    """
    src = """
async def h(session, thread):
    await session.refresh(thread, with_for_update=1)
"""
    assert _is_literal_true(_only_site_value(src)) is False


def test_strict_predicate_rejects_zero_which_sqlalchemy_reads_as_no_lock() -> None:
    """``with_for_update=0`` is rejected -- and this one is silent.

    SQLAlchemy's guard is ``with_for_update in (None, False)``, and
    ``0 == False``, so ``0`` yields **no lock at all** with no error
    (measured, 2.0.49). This file's counter compares with ``is``, so ``0``
    is *not* in its known-safe set and the site is still counted -- the
    count stays at one while the lock is gone. That gap between the two
    comparisons is exactly the space assert B occupies.
    """
    src = """
async def h(session, thread):
    await session.refresh(thread, with_for_update=0)
"""
    sites = _collect_row_lock_sites(src)
    assert len(sites) == 1, "``0`` must still be counted as a site by assert A"
    assert _is_literal_true(sites[0].value) is False


def test_strict_predicate_rejects_the_dict_options_family() -> None:
    """Every dict form is rejected, ``{"read": True}`` most of all.

    Measured SQL (2.0.49, postgresql dialect): ``{"read": True}`` ->
    ``FOR SHARE``, ``{"nowait": True}`` -> ``FOR UPDATE NOWAIT``,
    ``{"skip_locked": True}`` -> ``FOR UPDATE SKIP LOCKED``. Only the first
    is a silent correctness loss (share locks do not block each other, so
    both closes proceed and both return 2xx); the other two turn the race
    into an error rather than a wrong answer. All three are still refused,
    because "not the lock this code's correctness argument assumes" is the
    property being pinned, not "loud enough to notice".
    """
    for value in ('{"read": True}', '{"nowait": True}', '{"skip_locked": True}'):
        src = f"""
async def h(session, thread):
    await session.refresh(thread, with_for_update={value})
"""
        assert _is_literal_true(_only_site_value(src)) is False, value


def test_strict_predicate_rejects_every_non_constant_expression() -> None:
    """Dynamic forms are rejected without the checker evaluating them.

    ``some_flag`` (``Name``), ``bool(flag)`` (``Call``), ``a if c else b``
    (``IfExp``) and ``not off`` (``UnaryOp``) are all refused on the same
    ground: an ``ast.Constant`` is the only thing whose value is visible
    here. This is the whole undetected-regression shape msg-460 §3 named --
    a runtime-falsy expression keeps the site countable (assert A stays
    green) while the lock silently stops being taken.

    The rejection is by node type, deliberately. Do not extend this to fold
    constants or follow imports: a checker that starts proving some dynamic
    expressions safe has taken on the job assert A needs done the opposite
    way, and msg-460 §4 explains why one check cannot hold both.
    """
    for value in ("some_flag", "bool(flag)", "flag if flag else False", "not off"):
        src = f"""
async def h(session, thread, flag, off, some_flag):
    await session.refresh(thread, with_for_update={value})
"""
        assert _is_literal_true(_only_site_value(src)) is False, value


def test_strict_predicate_rejects_the_method_call_form() -> None:
    """``select(Thread).with_for_update()`` has no value node, so B refuses it.

    The method form is a real lock idiom and assert A counts it as a site --
    but it is not the shape this choke point is built on, and the difference
    is load-bearing rather than stylistic. ``post_message_in_session`` uses
    ``refresh`` precisely because a second ``select()`` would take the lock
    and then hand back the identity-map instance with its stale attributes
    intact, so the lock would be held while ``thread.status`` still read the
    pre-race value (the production comment says so at the call site).
    Swapping to the method form is therefore a change of design, not of
    spelling, and it should stop here and be argued for.
    """
    src = """
from sqlalchemy import select

def a(session):
    return session.execute(select(Thread).with_for_update()).scalar_one()
"""
    assert _is_literal_true(_only_site_value(src)) is False
