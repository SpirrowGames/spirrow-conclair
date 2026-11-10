"""Per-project loop control ORM (HOLD / RESUME).

Records, per project, the operator's *desired* autonomy state and the
state the loop *observed* when it last read it. The two are deliberately
separate columns: pressing a button in the UI only takes effect when the
loop next reads it, and the UI has to be able to say so rather than
imply the change was instant.

Write separation is enforced at the API layer, not here: ``desired_*``
is written only by ``PUT /control`` (operators) and ``observed_*`` only
by ``POST /control/observed`` (the loop). A loop able to move
``desired_*`` could silently resume a project someone had stopped.

``desired_*`` is nullable even though the spec drafted it NOT NULL. The
row has to be able to exist for a project nobody has configured, because
the loop reports ``observed`` for every project it runs -- including the
ones running on the default. "Never explicitly set" is therefore encoded
as ``desired_state IS NULL`` and surfaces as ``configured: false`` +
``desired_state: "run"`` (the default) in the API response. Absence of a
row means the same thing; both are "unconfigured", and neither is a
read failure.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from spirrow_conclair.models import Base

CONTROL_STATES = ("run", "supervised", "hold")

#: State assumed for a project with no explicit setting. Deliberately the
#: permissive one -- the point of the feature is "autonomous by default,
#: stopped on demand". Note this is *not* the fallback for a failed read:
#: consumers must treat a read failure as ``hold`` (INV-2), which is why
#: GET never 404s and the Magickit tools never fabricate a default.
DEFAULT_CONTROL_STATE = "run"

#: Thread statuses under which a thread may hold a Summon. A thread leaving
#: this set (or being deleted) loses its mark through the DB triggers added in
#: alembic revision 0011; ``PUT /control/summon`` refuses a thread outside it
#: with 409 ``summon_thread_not_open``. The same tuple is frozen into the
#: migration's SQL function, and an integration test compares the two.
SUMMON_OPEN_STATUSES = ("active", "awaiting_reply")

#: History actions for ``project_summon_history``.
SUMMON_ACTIONS = ("set", "clear")


class ProjectControl(Base):
    __tablename__ = "project_control"

    project: Mapped[str] = mapped_column(Text, primary_key=True)
    desired_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    observed_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Summon (revision 0011). All three NULL = no Summon; all three set =
    # one. The CHECK below forbids anything in between. Written only by
    # PUT /control/summon, POST /control/summon/clear and the
    # ``clear_active_summon`` trigger function -- never by the desired /
    # observed endpoints.
    active_summon_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_summon_since: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    active_summon_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"desired_state IS NULL OR desired_state IN {CONTROL_STATES}",
            name="project_control_desired_check",
        ),
        CheckConstraint(
            f"observed_state IS NULL OR observed_state IN {CONTROL_STATES}",
            name="project_control_observed_check",
        ),
        CheckConstraint(
            "(active_summon_thread_id IS NULL AND active_summon_since IS NULL"
            " AND active_summon_by IS NULL)"
            " OR (active_summon_thread_id IS NOT NULL AND active_summon_since IS NOT NULL"
            " AND active_summon_by IS NOT NULL"
            " AND btrim(active_summon_thread_id) <> '' AND btrim(active_summon_by) <> '')",
            name="project_control_active_summon_check",
        ),
    )


class ProjectControlHistory(Base):
    """Append-only record of desired-state changes.

    This is an audit trail, not an authorisation record: the tailnet is
    trusted and ``actor`` is whatever the caller typed. It answers "who
    said they stopped this, and when", which is what a two-person team
    actually needs from it.

    Only ``desired`` changes land here. ``observed`` reports arrive every
    round and would bury the operator actions they exist to explain.
    """

    __tablename__ = "project_control_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"state IN {CONTROL_STATES}",
            name="project_control_history_state_check",
        ),
        Index("idx_control_history_project", "project", "changed_at"),
    )


class ProjectSummonHistory(Base):
    """Append-only record of Summon set / clear.

    Kept apart from ``ProjectControlHistory``: that table's ``state`` is
    CHECKed to the desired-state vocabulary and answers a different question.
    ``actor`` is a record, not a credential; trigger-driven clears write
    ``system:status:<new_status>`` or ``system:thread_deleted``.
    ``note`` carries the optional reason Magickit's summon tools send.
    """

    __tablename__ = "project_summon_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"action IN {SUMMON_ACTIONS}",
            name="project_summon_history_action_check",
        ),
        Index("idx_summon_history_project", "project", "changed_at"),
    )
