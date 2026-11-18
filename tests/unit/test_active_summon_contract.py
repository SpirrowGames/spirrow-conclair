"""The ``active_summon`` contract and the Summon envelopes, without a database.

Pinned here (T-conclair-active-summon; spec magickit msg-1435 / msg-1514):

1. The fixture is the canonical file byte for byte (size + sha256, hard-coded).
2. The test-side classifier agrees with every fixture case, so it can be
   trusted to judge this server's responses.
3. ``ControlStateResponse`` renders a Summon that the contract reads as
   ``valid`` (with ``since`` as ``+00:00`` + six digits) and ``null`` as
   ``none`` -- the key is always present.
4. The open-status allow-list is the same in the model and in the migration
   that freezes it into SQL.
5. The Summon error envelopes: 409 / 422 with the code as ``error_type`` and
   as ``details.code``; other errors keep their class-name ``error_type``.
6. Blank / whitespace-only ``thread_id`` / ``by`` are refused with 422
   ``invalid_argument`` before the database is touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spirrow_conclair.api.control import router as control_router
from spirrow_conclair.api.error_handlers import register_error_handlers
from spirrow_conclair.db import get_session
from spirrow_conclair.exceptions import (
    ChatroomInvalidArgumentError,
    ChatroomStateError,
    ChatroomSummonStateError,
)
from spirrow_conclair.models.project_control import SUMMON_OPEN_STATUSES
from spirrow_conclair.models.thread import THREAD_STATUSES
from spirrow_conclair.schemas import ActiveSummon, ControlStateResponse
from tests.summon_contract import (
    FIXTURE,
    FIXTURE_SHA256,
    FIXTURE_SIZE,
    SERVER_SINCE_RE,
    classify,
    load_cases,
    normalize_expect,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- 1. byte identity -------------------------------------------------------


def test_fixture_is_the_canonical_copy_byte_for_byte() -> None:
    data = FIXTURE.read_bytes()
    assert len(data) == FIXTURE_SIZE
    assert hashlib.sha256(data).hexdigest() == FIXTURE_SHA256


def test_fixture_is_not_subject_to_eol_conversion() -> None:
    """A checkout with autocrlf must not turn LF into CRLF and break the sha."""
    attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "tests/data/summon/*.json -text" in attrs
    assert b"\r\n" not in FIXTURE.read_bytes()


# --- 2. the classifier agrees with the fixture -----------------------------


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["name"])
def test_classifier_matches_every_fixture_case(case: dict) -> None:
    assert classify(case["payload"]) == normalize_expect(case["expect"])


# --- 3. what this server renders -------------------------------------------


def _response(summon: ActiveSummon | None) -> dict:
    return ControlStateResponse(
        project="p",
        desired_state="run",
        desired_actor=None,
        desired_at=None,
        observed_state=None,
        observed_actor=None,
        observed_at=None,
        configured=False,
        active_summon=summon,
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "since",
    [
        datetime(2026, 10, 9, 1, 2, 3, tzinfo=timezone.utc),
        datetime(2026, 10, 9, 10, 2, 3, 123456, tzinfo=timezone(timedelta(hours=9))),
        datetime(2026, 10, 9, 1, 2, 3, 7, tzinfo=timezone.utc),
    ],
    ids=["whole-second", "jst-with-fraction", "one-microsecond"],
)
def test_a_summon_renders_as_contract_valid_with_server_since_format(
    since: datetime,
) -> None:
    body = _response(ActiveSummon(thread_id="T-a", since=since, by="takahito"))

    assert SERVER_SINCE_RE.match(body["active_summon"]["since"]), body
    assert classify(body) == {
        "kind": "valid",
        "thread_id": "T-a",
        "since": since.astimezone(timezone.utc),
        "by": "takahito",
    }


def test_no_summon_renders_as_an_explicit_null() -> None:
    body = _response(None)

    assert "active_summon" in body and body["active_summon"] is None
    assert classify(body) == {"kind": "none"}


# --- 4. the allow-list, in the model and in the migration ------------------


def _migration_open_statuses() -> tuple[str, ...]:
    path = REPO_ROOT / "alembic" / "versions" / "0011_active_summon.py"
    spec = importlib.util.spec_from_file_location("_mig_0011", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SUMMON_OPEN_STATUSES


def test_open_statuses_match_between_model_and_migration() -> None:
    assert _migration_open_statuses() == SUMMON_OPEN_STATUSES


def test_open_statuses_are_thread_statuses_and_exclude_resolved() -> None:
    assert set(SUMMON_OPEN_STATUSES) <= set(THREAD_STATUSES)
    assert "resolved" not in SUMMON_OPEN_STATUSES


# --- 5. envelopes ----------------------------------------------------------


@pytest.fixture
def envelope_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/summon/{code}")
    def _summon(code: str) -> None:
        raise ChatroomSummonStateError(code, "x", details={"active_summon": None})

    @app.get("/invalid")
    def _invalid() -> None:
        raise ChatroomInvalidArgumentError(
            "by must be a non-blank string",
            details={"code": "invalid_argument", "fields": ["by"]},
        )

    @app.get("/state")
    def _state() -> None:
        raise ChatroomStateError("plain")

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "code", ["summon_conflict", "summon_mismatch", "summon_thread_not_open"]
)
def test_summon_409_carries_its_code_twice(envelope_client: TestClient, code: str) -> None:
    r = envelope_client.get(f"/summon/{code}")

    assert r.status_code == 409
    body = r.json()
    assert body["error_type"] == code
    assert body["details"]["code"] == code
    assert body["details"]["active_summon"] is None


def test_invalid_argument_is_422(envelope_client: TestClient) -> None:
    r = envelope_client.get("/invalid")

    assert r.status_code == 422
    assert r.json() == {
        "error_type": "invalid_argument",
        "error": "by must be a non-blank string",
        "details": {"code": "invalid_argument", "fields": ["by"]},
    }


def test_other_errors_keep_the_class_name(envelope_client: TestClient) -> None:
    """The override is opt-in: nothing else's wire string moved."""
    assert envelope_client.get("/state").json()["error_type"] == "ChatroomStateError"


# --- 6. blank fields never reach the database ------------------------------


@pytest.fixture
def control_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(control_router)

    class _NoDB:
        def __getattr__(self, name: str):  # pragma: no cover - must not run
            raise AssertionError(f"the database was touched ({name})")

    async def _no_session() -> AsyncIterator[object]:
        yield _NoDB()

    app.dependency_overrides[get_session] = _no_session
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("blank", ["", " ", "\t", " \n "])
@pytest.mark.parametrize("field", ["thread_id", "by"])
@pytest.mark.parametrize(
    ("method", "path"),
    [("PUT", "/v1/projects/p/control/summon"), ("POST", "/v1/projects/p/control/summon/clear")],
)
def test_blank_fields_are_422_invalid_argument(
    control_client: TestClient, method: str, path: str, field: str, blank: str
) -> None:
    body = {"thread_id": "T-a", "by": "takahito", field: blank}
    r = control_client.request(method, path, json=body)

    assert r.status_code == 422, r.text
    assert r.json()["error_type"] == "invalid_argument"
    assert r.json()["details"] == {"code": "invalid_argument", "fields": [field]}
