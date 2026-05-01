"""msg_id format / parse (pure).

`allocate_next_msg_id` requires a real DB session and is covered by
the integration tests in T10 instead.
"""

from __future__ import annotations

import pytest

from spirrow_conclair.exceptions import ChatroomIntegrityError
from spirrow_conclair.services.msg_id_allocator import format_msg_id, parse_msg_id


# ---- format ----------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "msg-001"),
        (9, "msg-009"),
        (99, "msg-099"),
        (100, "msg-100"),
        (999, "msg-999"),
        (1000, "msg-1000"),
        (12345, "msg-12345"),
        (99999, "msg-99999"),
        (100000, "msg-100000"),
    ],
)
def test_format_zero_padding(n: int, expected: str) -> None:
    assert format_msg_id(n) == expected


@pytest.mark.parametrize("n", [0, -1, -100])
def test_format_rejects_non_positive(n: int) -> None:
    with pytest.raises(ChatroomIntegrityError):
        format_msg_id(n)


# ---- parse -----------------------------------------------------------


@pytest.mark.parametrize(
    "s,n",
    [
        ("msg-001", 1),
        ("msg-009", 9),
        ("msg-100", 100),
        ("msg-1000", 1000),
        ("msg-99999", 99999),
        # leading zeros beyond the minimum width are still valid input
        ("msg-0000005", 5),
    ],
)
def test_parse_valid(s: str, n: int) -> None:
    assert parse_msg_id(s) == n


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "msg-",
        "msg-x",
        "msg-1a",
        "MSG-001",
        "thread-001",
        "001",
        "msg-1.5",
        "msg-001 ",  # trailing whitespace
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(ChatroomIntegrityError):
        parse_msg_id(bad)


# ---- round-trip ------------------------------------------------------


@pytest.mark.parametrize("n", [1, 5, 99, 100, 999, 1000, 99999, 1_000_000])
def test_round_trip(n: int) -> None:
    assert parse_msg_id(format_msg_id(n)) == n
