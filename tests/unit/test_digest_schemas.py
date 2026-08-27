"""Unit tests for the thread-digest request schema (no DB).

The cross-field rule lives in a pydantic validator rather than in the route
body so the caller gets a 422 naming the field instead of the 409 the CHECK
constraint would produce. These tests pin that the two agree about what is
legal, since the constraint is only the backstop.
"""

from __future__ import annotations

import warnings

import pytest
from pydantic import ValidationError

from spirrow_conclair.models.digest import DEFAULT_DIGEST_STYLE, DIGEST_SCOPES
from spirrow_conclair.schemas.digest import PutThreadDigestRequest

BASE = {
    "digest": "Bohr が X 方式を提案、Heisenberg が実装。Einstein が Y を指摘。",
    "source_last_msg_id": "msg-042",
    "source_msg_count": 18,
    "producer": "magickit-digest-sweeper",
}


def _req(**overrides: object) -> PutThreadDigestRequest:
    return PutThreadDigestRequest(**{**BASE, **overrides})  # type: ignore[arg-type]


# ---- scope / target_msg_id coherence ----------------------------------


def test_thread_scope_needs_no_target() -> None:
    req = _req()
    assert req.scope == "thread"
    assert req.target_msg_id is None


def test_message_scope_with_a_target_is_valid() -> None:
    req = _req(scope="message", target_msg_id="msg-041")
    assert req.scope == "message"
    assert req.target_msg_id == "msg-041"


def test_message_scope_without_a_target_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _req(scope="message")
    assert "target_msg_id is required" in str(exc.value)


def test_thread_scope_with_a_target_is_rejected() -> None:
    """A whole-thread digest that names one message is two claims, not one."""
    with pytest.raises(ValidationError) as exc:
        _req(target_msg_id="msg-041")
    assert "must be omitted" in str(exc.value)


def test_message_scope_with_a_blank_target_is_rejected() -> None:
    """`str_strip_whitespace` turns "  " into "", which must not pass as a target."""
    with pytest.raises(ValidationError):
        _req(scope="message", target_msg_id="   ")


def test_an_unknown_scope_is_rejected_by_the_literal() -> None:
    with pytest.raises(ValidationError):
        _req(scope="project")


def test_the_literal_matches_the_orm_constant() -> None:
    """One vocabulary, declared twice; keep them from drifting."""
    from typing import get_args

    from spirrow_conclair.schemas.digest import DigestScope

    assert set(get_args(DigestScope)) == set(DIGEST_SCOPES)


# ---- the schema and the CHECK constraints agree -----------------------


def test_a_blank_digest_is_rejected() -> None:
    """Matches the DB's ``length(btrim(digest)) > 0``.

    `str_strip_whitespace` runs before `min_length`, so whitespace-only
    fails here for the same reason it would fail there.
    """
    with pytest.raises(ValidationError):
        _req(digest="   ")


def test_a_zero_source_msg_count_is_rejected() -> None:
    """Matches the DB's ``source_msg_count >= 1``.

    A digest of nothing is not a digest; the producer's floor is far higher.
    """
    with pytest.raises(ValidationError):
        _req(source_msg_count=0)


def test_negative_provenance_numbers_are_rejected() -> None:
    for field in ("source_chars", "input_tokens", "output_tokens", "duration_ms"):
        with pytest.raises(ValidationError):
            _req(**{field: -1})


# ---- defaults ---------------------------------------------------------


def test_defaults() -> None:
    req = _req()
    assert req.style == DEFAULT_DIGEST_STYLE
    assert req.truncated is False
    assert req.model is None
    assert req.tier is None


def test_a_blank_style_is_rejected() -> None:
    """`style` is part of both uniqueness keys, so it cannot be empty."""
    with pytest.raises(ValidationError):
        _req(style="  ")


# ---- pydantic protected namespace ------------------------------------


def test_the_model_field_emits_no_protected_namespace_warning() -> None:
    """`model`, not `model_name`.

    pydantic v2 defaults to ``protected_namespaces=('model_',)``, and
    ``"model".startswith("model_")`` is False while ``"model_name"`` is not --
    counter-intuitive enough that the field name is pinned by a test rather
    than by a comment alone.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        req = _req(model="light", tier="light")
    assert req.model == "light"
    assert req.tier == "light"
