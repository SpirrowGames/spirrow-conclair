"""Attribution for a close posted by someone other than the thread owner.

Invariant 3 refuses ``closes_thread`` from a non-owner, and
``assert_closes_thread_rule`` relaxes exactly one clause of it when
``owner_override`` is set (ADR-2026-06-04-19 D-5). Two different bypasses ride
that one boolean: a human Tier-C force-close, and the PR-gate ledger carve-out
Magickit performs after verifying a merged PR against an approving review.
Both arrive here as ``True`` and nothing about *which* survives the write, so
``GET /integrity`` reported every sanctioned close as
``closes_thread_by_non_owner`` forever -- 45 of them in ``spirrow-playproof``
alone, of which 2 were force-closes and 43 were routine carve-outs. A count
that grows with normal development cannot be a health signal.

What this module adds is not new evidence. Magickit already holds the
structured grounds at the moment it decides (a ``LedgerVerdict``, or a human's
reason string) and projects them onto one bit. This records the projection it
was throwing away.

**Conclair does not verify the claim.** It records what the caller asserted and
never asks GitHub whether the PR really merged (D-3: Conclair stays a leaf and
pulls no cross-service state -- the same boundary ``role`` and
``next_participant`` already draw). The value is not "verified" but
*re-derivable*: ``merged_head`` + ``approving_review_id`` let a reader check
the claim later against the system that does know.

Four outcomes, not two
----------------------

A missing record is not a verdict. Rows closed before the recorder existed
cannot be called sanctioned *or* corrupt, so they get their own bucket instead
of being guessed at:

===========================================  =============================
condition                                    outcome
===========================================  =============================
record present, kind in SANCTIONED_KINDS     ``sanctioned`` (counted only)
record present, any other kind               ``unattributable``
                                             (``unclassified_override``)
no record, msg at/after the cutover          ``corruption`` (a real issue)
no record, msg before the cutover / no       ``unattributable``
cutover configured                           (``pre_recording``)
===========================================  =============================

Two properties of that table are load-bearing:

* **The first split is on the record, not on the clock.** A legacy
  ``owner_override`` bool arriving during a deploy skew window is recorded as
  ``kind="unspecified"`` and is ``unattributable`` *whatever its timestamp* --
  so bringing Conclair up before Magickit cannot manufacture corruption
  findings. The clock is consulted only where no record exists at all.
* **The two unattributable reasons are kept apart.** ``pre_recording`` freezes
  once the recorder ships; ``unclassified_override`` should fall to zero once
  every caller sends a sanction, and a non-zero count afterwards means a call
  site is still passing the bare boolean. Merged into one bucket, that signal
  is invisible.

``corruption`` means only "this row did not come through the write path": the
recorder sits at the single choke point both close routes share
(``post_message_in_session``), so after the cutover any accepted non-owner
close writes *something*. Rolling Conclair back re-opens that gap, and findings
from such a window are true positives, not noise -- ``/integrity`` is a report,
never an enforcement point, so the cost of noticing is attention.

Timestamps come from the **message**, never from its audit event. The rows this
classification exists to catch are exactly the ones that never produced an
event, so reading a time off the event would make the detector depend on the
artifact whose absence it is detecting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

# Key under a `chatroom_events.details` object. The event row's own `msg_id`
# column carries the attribution -- the sanction speaks about one message, and
# a thread-level association would hand one thread's sanction to every closing
# message in it.
CLOSE_SANCTION_KEY = "close_sanction"

CloseSanctionKind = Literal["human_override", "pr_gate_ledger", "unspecified"]

#: Kinds that make a non-owner close accounted for. `unspecified` is
#: deliberately absent: it says a bypass happened and nothing about which.
SANCTIONED_KINDS: tuple[str, ...] = ("human_override", "pr_gate_ledger")

#: Recorded when a caller passes only the legacy `owner_override` boolean.
UNSPECIFIED_SANCTION: dict[str, Any] = {"kind": "unspecified"}

CloseVerdict = Literal["sanctioned", "unattributable", "corruption"]
UnattributableReason = Literal["pre_recording", "unclassified_override"]


@dataclass(frozen=True)
class SanctionRecord:
    """A ``close_sanction`` object found on an audit event.

    ``kind`` is ``None`` when the record exists but cannot be read (not an
    object, or no usable ``kind``). That is still a record: the write path did
    run, so the row is not corruption -- it is unattributable.
    """

    kind: str | None


@dataclass(frozen=True)
class CloseClassification:
    verdict: CloseVerdict
    #: The recorded kind, when one was readable.
    kind: str | None = None
    #: Set only when ``verdict == "unattributable"``.
    reason: UnattributableReason | None = None


def read_sanction_record(details: Any) -> SanctionRecord | None:
    """Extract the sanction from an event's ``details``, or ``None``.

    Returns ``None`` only when the key is absent -- a present-but-malformed
    value yields a ``SanctionRecord`` with ``kind=None`` so it is treated as
    unattributable rather than as a row that skipped the write path.
    """
    if not isinstance(details, dict):
        return None
    if CLOSE_SANCTION_KEY not in details:
        return None
    raw = details[CLOSE_SANCTION_KEY]
    if not isinstance(raw, dict):
        return SanctionRecord(kind=None)
    kind = raw.get("kind")
    return SanctionRecord(kind=kind if isinstance(kind, str) and kind else None)


def _as_utc(value: datetime) -> datetime:
    """Naive input is read as UTC.

    ``messages.timestamp`` is ``TIMESTAMP WITH TIME ZONE`` so it always
    arrives aware, but the cutover is operator-supplied configuration and
    ``SANCTION_RECORDING_SINCE=2026-09-06T00:00:00`` is the form a person
    writes. Comparing that against an aware timestamp raises, which would take
    the endpoint down over a punctuation choice.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def classify_non_owner_close(
    *,
    record: SanctionRecord | None,
    msg_timestamp: datetime,
    sanction_recording_since: datetime | None,
) -> CloseClassification:
    """Sort one non-owner close into the four buckets documented above.

    Args:
        record: The sanction found for *this message*, or ``None`` if none was.
        msg_timestamp: ``messages.timestamp`` -- the one entity guaranteed to
            exist here, since it is the row being classified.
        sanction_recording_since: Cutover instant. ``None`` means the deployment
            has not declared one, and nothing is called corruption: without a
            cutover, "no record" is indistinguishable from "recorder not yet
            live", and inventing a boundary would fabricate findings.
    """
    if record is not None:
        if record.kind in SANCTIONED_KINDS:
            return CloseClassification(verdict="sanctioned", kind=record.kind)
        return CloseClassification(
            verdict="unattributable",
            kind=record.kind,
            reason="unclassified_override",
        )

    if sanction_recording_since is None:
        return CloseClassification(verdict="unattributable", reason="pre_recording")

    if _as_utc(msg_timestamp) >= _as_utc(sanction_recording_since):
        return CloseClassification(verdict="corruption")

    return CloseClassification(verdict="unattributable", reason="pre_recording")
