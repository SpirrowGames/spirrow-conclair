"""Unit tests for the validation-error envelope.

One rule, and it is about the worst shape this handler can take: **the
endpoint whose job is to say what you got wrong must not be the one that
breaks.** pydantic v2 puts the original exception object into ``ctx["error"]``
when a ``@model_validator`` raises, so ``exc.errors()`` is not JSON-safe --
and a 422 that fails to render becomes a 500 that says nothing.
"""

from __future__ import annotations

from spirrow_conclair.api.error_handlers import _jsonable


def test_scalars_pass_through_unchanged() -> None:
    for value in ("s", 1, 1.5, True, None):
        assert _jsonable(value) is value or _jsonable(value) == value


def test_an_exception_becomes_its_message() -> None:
    """The case that broke: a live ValueError inside `ctx`."""
    err = ValueError("target_msg_id must be omitted when scope is 'thread'")

    assert _jsonable(err) == "target_msg_id must be omitted when scope is 'thread'"


def test_a_realistic_pydantic_error_list_survives() -> None:
    """Shaped like the real `exc.errors()` from a failing model_validator."""
    errors = [
        {
            "type": "value_error",
            "loc": ("body",),
            "msg": "Value error, target_msg_id must be omitted",
            "input": {"digest": "要約", "target_msg_id": "msg-001"},
            "ctx": {"error": ValueError("target_msg_id must be omitted")},
        }
    ]

    out = _jsonable(errors)

    import json

    # The whole point: this must not raise.
    rendered = json.dumps(out, ensure_ascii=False)
    assert "target_msg_id must be omitted" in rendered
    # Tuples become lists so `loc` survives as JSON.
    assert out[0]["loc"] == ["body"]
    assert out[0]["ctx"]["error"] == "target_msg_id must be omitted"
    # The caller's own input is preserved, not flattened to a string.
    assert out[0]["input"]["digest"] == "要約"


def test_a_field_constraint_error_is_untouched() -> None:
    """The shape that always worked must keep working.

    Every schema here used only `Field(...)` constraints until the digest
    cross-field rule, which is why the bug went unnoticed.
    """
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", "digest"),
            "msg": "String should have at least 1 character",
            "input": "",
            "ctx": {"min_length": 1},
        }
    ]

    out = _jsonable(errors)

    assert out[0]["ctx"] == {"min_length": 1}
    assert out[0]["input"] == ""


def test_nesting_is_handled_all_the_way_down() -> None:
    out = _jsonable({"a": [{"b": (ValueError("deep"),)}]})

    assert out == {"a": [{"b": ["deep"]}]}


def test_a_non_string_key_is_stringified() -> None:
    assert _jsonable({1: "x"}) == {"1": "x"}
