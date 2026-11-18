"""Schemas for the per-project loop control endpoints.

Six endpoints land here:
- ``GET  /v1/projects/{project}/control``           (``ControlStateResponse``)
- ``PUT  /v1/projects/{project}/control``           (``SetControlRequest``)
- ``POST /v1/projects/{project}/control/observed``  (``ReportObservedRequest``)
- ``GET  /v1/projects/{project}/control/history``   (``ControlHistoryListResponse``)
- ``PUT  /v1/projects/{project}/control/summon``          (``SummonRequest``)
- ``POST /v1/projects/{project}/control/summon/clear``    (``SummonRequest``)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

#: Mirrors ``models.project_control.CONTROL_STATES``. Declared as a
#: Literal so FastAPI rejects an unknown value with 422 before the route
#: body runs -- the CHECK constraint is the backstop, not the gate.
ControlState = Literal["run", "supervised", "hold"]


class SetControlRequest(BaseModel):
    """PUT /control body -- sets ``desired``.

    ``actor`` is a record of who pressed the button, not an authenticated
    identity. The tailnet is the trust boundary (P-3); anything reaching
    this endpoint is already trusted to change the value.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    state: ControlState
    actor: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class ReportObservedRequest(BaseModel):
    """POST /control/observed body -- the loop reporting what it read.

    Deliberately carries no way to express a desired state. The write
    separation is a tool/endpoint boundary so that "don't let the loop
    resume itself" can later be enforced by simply not handing the loop
    the other endpoint.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    state: ControlState
    actor: str = Field(min_length=1, max_length=200)


class SummonRequest(BaseModel):
    """PUT /control/summon and POST /control/summon/clear body.

    ``thread_id`` and ``by`` have no length constraint here on purpose: a
    blank or whitespace-only value must come back as 422 with
    ``error_type = "invalid_argument"`` (the Summon contract), which the
    route raises itself, not as FastAPI's generic ``ValidationError``.
    ``by`` is a record of who said they did it, not a credential.
    ``note`` is optional and kept in ``project_summon_history``; Magickit's
    ``summon_set`` / ``summon_clear`` send it.
    """

    thread_id: str = Field(max_length=200)
    by: str = Field(max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class ActiveSummon(BaseModel):
    """The project's Summon, as ``active_summon`` in the control response.

    ``since`` is rendered as UTC with an explicit ``+00:00`` offset and
    exactly six fractional digits (msg-1435), not pydantic's default ``Z``
    with a variable fraction. Both satisfy the contract fixture
    (``tests/data/summon/active_summon_contract.json``); the fixed form is
    the one this server promises.
    """

    thread_id: str
    since: datetime
    by: str

    @field_serializer("since")
    def _since_utc_micro(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class ControlStateResponse(BaseModel):
    """Current control state for a project.

    Returned by GET, PUT and POST /observed alike so every caller sees
    the same shape.

    ``configured`` is False when nobody has set a state for this project.
    In that case ``desired_state`` still carries the effective default
    (``run``) while ``desired_actor`` / ``desired_at`` are null -- the
    caller gets a usable value *and* can tell it was never set. This
    endpoint never 404s, so a 4xx/5xx from it unambiguously means "read
    failed", which consumers must treat as ``hold``.
    """

    model_config = ConfigDict(from_attributes=True)

    project: str
    desired_state: ControlState
    desired_actor: str | None
    desired_at: datetime | None
    observed_state: ControlState | None
    observed_actor: str | None
    observed_at: datetime | None
    configured: bool
    #: Always present; ``null`` means no Summon (contract case "null").
    active_summon: ActiveSummon | None


class ControlHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: ControlState
    actor: str
    changed_at: datetime
    note: str | None


class ControlHistoryListResponse(BaseModel):
    """Most recent desired-state changes, newest first."""

    items: list[ControlHistoryItem]
    total: int
    limit: int
