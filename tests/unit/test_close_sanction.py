"""Classification of a non-owner close (services.close_sanction).

The rule this pins is that the *record* decides first and the clock only
breaks ties among rows that have none. Both halves have a failure mode the
other cannot catch: classifying on the clock first turns a deploy skew window
into fabricated corruption findings, and never consulting it at all leaves a
row that bypassed the write path indistinguishable from a legacy one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from spirrow_conclair.schemas.message import CloseSanction
from spirrow_conclair.services.close_sanction import (
    CLOSE_SANCTION_KEY,
    SanctionRecord,
    classify_non_owner_close,
    read_sanction_record,
)

CUTOVER = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
BEFORE = CUTOVER - timedelta(days=1)
AFTER = CUTOVER + timedelta(days=1)


# ----- reading a record off an event's details -----


def test_absent_key_is_no_record() -> None:
    assert read_sanction_record({"type": "decide"}) is None


def test_non_dict_details_is_no_record() -> None:
    assert read_sanction_record(None) is None
    assert read_sanction_record("close_sanction") is None


@pytest.mark.parametrize("kind", ["human_override", "pr_gate_ledger", "unspecified"])
def test_readable_record_yields_its_kind(kind: str) -> None:
    record = read_sanction_record({CLOSE_SANCTION_KEY: {"kind": kind}})
    assert record == SanctionRecord(kind=kind)


@pytest.mark.parametrize(
    "raw",
    [
        "pr_gate_ledger",  # not an object
        {},  # object without a kind
        {"kind": ""},  # empty kind
        {"kind": 7},  # non-string kind
    ],
)
def test_present_but_unreadable_is_still_a_record(raw: object) -> None:
    """A malformed record still proves the write path ran.

    Returning ``None`` here would send the row down the timestamp branch and,
    after the cutover, report it as corruption -- a false positive about the
    one thing this bucket is supposed to mean.
    """
    assert read_sanction_record({CLOSE_SANCTION_KEY: raw}) == SanctionRecord(kind=None)


# ----- the four buckets -----


@pytest.mark.parametrize("kind", ["human_override", "pr_gate_ledger"])
@pytest.mark.parametrize("ts", [BEFORE, AFTER])
def test_sanctioned_kinds_are_counted_not_reported(kind: str, ts: datetime) -> None:
    result = classify_non_owner_close(
        record=SanctionRecord(kind=kind),
        msg_timestamp=ts,
        sanction_recording_since=CUTOVER,
    )
    assert result.verdict == "sanctioned"
    assert result.kind == kind


@pytest.mark.parametrize("ts", [BEFORE, AFTER])
def test_unspecified_is_unattributable_regardless_of_the_clock(ts: datetime) -> None:
    """The deploy-skew guarantee, as a test.

    Bringing Conclair up before Magickit means legacy `owner_override` calls
    keep arriving and are recorded as `unspecified`. If the clock could
    override that, every such call after the cutover would be reported as
    corruption and the skew window would be un-survivable.
    """
    result = classify_non_owner_close(
        record=SanctionRecord(kind="unspecified"),
        msg_timestamp=ts,
        sanction_recording_since=CUTOVER,
    )
    assert result.verdict == "unattributable"
    assert result.reason == "unclassified_override"


def test_unknown_kind_is_unattributable_not_corruption() -> None:
    result = classify_non_owner_close(
        record=SanctionRecord(kind="something_new"),
        msg_timestamp=AFTER,
        sanction_recording_since=CUTOVER,
    )
    assert result.verdict == "unattributable"
    assert result.reason == "unclassified_override"


def test_no_record_after_cutover_is_corruption() -> None:
    result = classify_non_owner_close(
        record=None, msg_timestamp=AFTER, sanction_recording_since=CUTOVER
    )
    assert result.verdict == "corruption"


def test_no_record_exactly_at_the_cutover_is_corruption() -> None:
    """The boundary is closed on the recording side.

    `sanction_recording_since` means "from this instant the recorder is live",
    so a msg written at it should have a record.
    """
    result = classify_non_owner_close(
        record=None, msg_timestamp=CUTOVER, sanction_recording_since=CUTOVER
    )
    assert result.verdict == "corruption"


def test_no_record_before_cutover_is_pre_recording() -> None:
    result = classify_non_owner_close(
        record=None, msg_timestamp=BEFORE, sanction_recording_since=CUTOVER
    )
    assert result.verdict == "unattributable"
    assert result.reason == "pre_recording"


def test_without_a_configured_cutover_nothing_is_corruption() -> None:
    """Unset config disarms the strictest bucket rather than guessing.

    The response carries `sanction_recording_since` so this state is visible
    to whoever reads the report.
    """
    result = classify_non_owner_close(
        record=None, msg_timestamp=AFTER, sanction_recording_since=None
    )
    assert result.verdict == "unattributable"
    assert result.reason == "pre_recording"


def test_naive_cutover_is_read_as_utc() -> None:
    """`SANCTION_RECORDING_SINCE=2026-09-06T12:00:00` is what a person types.

    Comparing a naive config value against an aware `messages.timestamp`
    raises TypeError, which would take a report endpoint down over a
    punctuation choice.
    """
    result = classify_non_owner_close(
        record=None,
        msg_timestamp=AFTER,
        sanction_recording_since=CUTOVER.replace(tzinfo=None),
    )
    assert result.verdict == "corruption"


# ----- the wire shape -----


def test_ledger_sanction_requires_its_evidence() -> None:
    """Evidence is what makes bucket ① worth anything.

    Conclair never checks whether the PR merged (it holds no GitHub state);
    the claim's value is that `merged_head` + `approving_review_id` let a
    reader check it later. A claim without them is unfalsifiable, and
    `chatroom_events` is append-only, so it is refused at the boundary rather
    than stored forever.
    """
    with pytest.raises(ValidationError) as ei:
        CloseSanction(kind="pr_gate_ledger", pr="SpirrowGames/x#7")
    assert "merged_head" in str(ei.value)


def test_ledger_sanction_with_full_evidence_is_accepted() -> None:
    sanction = CloseSanction(
        kind="pr_gate_ledger",
        pr="SpirrowGames/spirrow-playproof#57",
        merged_head="deadbee",
        approving_review_id="PRR_1",
    )
    assert sanction.model_dump(exclude_none=True) == {
        "kind": "pr_gate_ledger",
        "pr": "SpirrowGames/spirrow-playproof#57",
        "merged_head": "deadbee",
        "approving_review_id": "PRR_1",
    }


def test_human_override_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        CloseSanction(kind="human_override")


def test_kinds_do_not_borrow_each_others_evidence() -> None:
    """A mixed claim is two claims, and the audit would have to pick one."""
    with pytest.raises(ValidationError):
        CloseSanction(kind="human_override", reason="stale owner", pr="x#1")
    with pytest.raises(ValidationError):
        CloseSanction(kind="unspecified", reason="stale owner")
