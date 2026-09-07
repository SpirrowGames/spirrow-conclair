"""The close-sanction vocabulary has one declaration site, and the wire is
derived from it.

Two properties are pinned here, and both survive a mutation of the SOT alone:

* **Exhaustiveness (b).** ``KIND_IS_SANCTIONED`` is a total mapping over
  ``CloseSanctionKind``. Adding a token to the Literal without extending the
  map turns this test red, because the map's keys and the Literal's members
  are compared as sets. This is the receipt for the fail-loud domain-side
  posture: a missing entry means a runtime ``KeyError`` at
  ``classify_non_owner_close``, and this test names the drift before that
  ever runs.

* **Wire derivation (c).** ``CloseSanction`` accepts *every* member of
  ``CloseSanctionKind`` and only those, and it is checked by iterating
  ``get_args(CloseSanctionKind)`` -- so the test itself never re-declares the
  vocabulary. That is what keeps it compatible with AC1 (a single declaration
  site): checking derivation without copying the vocabulary is the only shape
  that can.

The two tests are on *different layers* on purpose. Composed together as an
end-to-end run, a mutation of the SOT alone would produce a 500 at the API
boundary (the schema accepts the new kind, the domain map raises ``KeyError``)
-- observable, but ambiguous: which layer broke? Split, the answer is exact:
if (b) is red the domain map is behind, if (c) is red the wire is not
deriving from the SOT.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from spirrow_conclair.schemas.close_sanction_vocab import (
    KIND_IS_SANCTIONED,
    CloseSanctionKind,
    UnattributableReason,
)
from spirrow_conclair.schemas.message import CloseSanction

# ----- (b) exhaustiveness of the domain map over the wire vocabulary -----


def test_kind_is_sanctioned_covers_every_kind() -> None:
    """The map's keys are exactly the Literal's members.

    Compared as *sets* rather than "kind in map": a set difference report
    names both directions of drift in one assertion -- a token the map is
    missing (the fail-loud path we care about) or a stale key that outlived
    a kind's removal (which would leave a lookup that answers a question no
    caller can ask).
    """
    assert set(KIND_IS_SANCTIONED.keys()) == set(get_args(CloseSanctionKind))


def test_no_kind_maps_to_a_non_bool() -> None:
    """Every value is a real ``bool``.

    ``KIND_IS_SANCTIONED[kind]`` feeds directly into an ``if`` in
    ``classify_non_owner_close``, so a truthy string like ``"maybe"`` would
    quietly re-introduce the sanctioned-by-default behaviour this map exists
    to remove.
    """
    for kind, is_sanctioned in KIND_IS_SANCTIONED.items():
        assert isinstance(is_sanctioned, bool), (
            f"{kind!r} maps to {is_sanctioned!r}, not a bool"
        )


def test_unspecified_is_not_sanctioned() -> None:
    """The one classification we spell out in test, because it is the
    load-bearing behaviour: a caller who sends only the legacy boolean
    (recorded as ``unspecified``) must fall into ``unattributable``, never
    into ``sanctioned``. Merging that into the sanctioned bucket would put
    unclassified overrides back onto the "counted, not reported" side and
    hide exactly the signal ``unclassified_override`` exists to raise.
    """
    assert KIND_IS_SANCTIONED["unspecified"] is False


# ----- (c) wire derivation from the SOT -----


@pytest.mark.parametrize("kind", list(get_args(CloseSanctionKind)))
def test_every_kind_passes_close_sanction_validation(kind: CloseSanctionKind) -> None:
    """The wire accepts every ``CloseSanctionKind`` member.

    Iterating ``get_args`` here (not writing the kinds out) is what makes
    this test the *derivation* proof: if a member is added to the SOT and
    ``schemas/message.py`` still hardcodes an inline Literal, the new member
    is fed to a schema that will refuse it and the parametrisation fails at
    that member's row.

    Evidence is supplied per kind because ``CloseSanction`` also runs an
    intra-model validator (evidence must match kind); testing derivation
    means we make each kind's construction succeed, not that we probe the
    evidence rules -- those are already covered in ``test_close_sanction``.
    """
    payload: dict[str, object] = {"kind": kind}
    if kind == "human_override":
        payload["reason"] = "Tier-C force close"
    elif kind == "pr_gate_ledger":
        payload["pr"] = "SpirrowGames/x#1"
        payload["merged_head"] = "deadbee"
        payload["approving_review_id"] = "PRR_1"
    # "unspecified" carries no evidence.

    sanction = CloseSanction(**payload)
    assert sanction.kind == kind


def test_a_kind_outside_the_vocabulary_is_refused() -> None:
    """The other side of derivation: only members pass.

    A representative unknown value stands in for "any string that is not in
    ``get_args(CloseSanctionKind)``". If the wire ever stops being derived
    from the SOT (a broadening to ``str`` for compatibility, say), this
    stops raising.
    """
    with pytest.raises(ValidationError) as ei:
        CloseSanction(kind="not_a_real_kind")
    # The message form matters only insofar as it names the field. What we
    # are actually asserting is that a ValidationError happens at all.
    assert "kind" in str(ei.value)


# ----- The UnattributableReason vocabulary has the same shape (a
# single-declaration property, no domain-side map to keep in sync) -----


def test_unattributable_reasons_are_the_expected_two() -> None:
    """The reason vocabulary is small and stable.

    Adding a value here would ordinarily be silent from the wire's side
    (``UnattributableClose.reason`` just widens), but the fallback in
    ``services/integrity.py`` (``classification.reason or "pre_recording"``)
    would keep landing on the old default. This test freezes the set so a
    change here forces the fallback to be revisited alongside it.
    """
    assert set(get_args(UnattributableReason)) == {
        "pre_recording",
        "unclassified_override",
    }
