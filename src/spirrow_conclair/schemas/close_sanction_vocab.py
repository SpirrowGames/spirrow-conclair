"""Leaf module: the wire vocabulary for close-sanction attribution.

Every Literal token that names a close-sanction *kind* or an
*unattributable* reason is declared here, once. The `services.close_sanction`
module owns the *meaning* of these tokens (which are sanctioned, how they map
to the four buckets); this module owns nothing but the names, so both the wire
layer (`schemas/message.py`, `schemas/event.py`) and the domain layer
(`services/close_sanction.py`, `services/integrity.py`) can import them
without either layer depending on the other.

Two rules make this file work:

* **It imports nothing but `typing`.** A leaf is a leaf only if nothing beneath
  it can pull the rest of the world in. Anything richer than a `Literal`
  belongs elsewhere.
* **`KIND_IS_SANCTIONED` is a total mapping over `CloseSanctionKind`.** Its
  keys are exactly `get_args(CloseSanctionKind)` -- the `unit` exhaustiveness
  test refuses to run otherwise. Dropping the fallback default (previously a
  `frozenset` membership check that silently returned False for any unknown
  kind) is what turns a forgotten mapping update into a loud `KeyError` at
  the site of use rather than a silent under-count in `sanctioned_counts`.
"""

from __future__ import annotations

from typing import Literal

#: Wire vocabulary for a `close_sanction.kind`. Kept as an inline `Literal`
#: (not a `str` `Enum`) so pydantic's own JSON-Schema and error messages call
#: the values by their string form -- the API's public contract.
CloseSanctionKind = Literal["human_override", "pr_gate_ledger", "unspecified"]

#: Wire vocabulary for `UnattributableClose.reason`.
UnattributableReason = Literal["pre_recording", "unclassified_override"]

#: Total mapping from `CloseSanctionKind` to whether the kind is
#: *sanctioned* (i.e. accounted for and counted, not reported as an issue).
#:
#: This is deliberately a `dict`, not a `set`. A set encodes "sanctioned" as
#: membership and "not sanctioned" as absence, so a newly added kind that
#: nobody remembered to classify silently falls to the "not sanctioned" side
#: -- exactly the failure mode that made `sanctioned_counts` unreliable
#: before this module existed. A dict lookup on a missing key raises,
#: which is what the `test_kind_is_sanctioned_covers_every_kind` unit test
#: pins.
#:
#: `unspecified` is `False` on purpose: it says a bypass happened and
#: nothing about *which*, so it belongs in the unattributable bucket.
KIND_IS_SANCTIONED: dict[CloseSanctionKind, bool] = {
    "human_override": True,
    "pr_gate_ledger": True,
    "unspecified": False,
}
