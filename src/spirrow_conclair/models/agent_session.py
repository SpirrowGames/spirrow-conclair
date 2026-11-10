"""Agent session ORM (T45 PR1).

Maps a Claude Code session UUID to the agent running it. A row is
write-once, except for ``last_declared_at``, ``transcript_path`` (NULL ->
value, once) and the end fields. Those rules live in
``services.agent_session`` and nowhere else.

``mindwire_session_id`` is stored but the API never returns it: it is the
proof a loop writer presents, and the board must not show it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from spirrow_conclair.models import Base

AGENT_SESSION_SOURCES = ("loop", "hook")


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    cc_session_uuid: Mapped[str] = mapped_column(Text, primary_key=True)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    identity: Mapped[str | None] = mapped_column(Text, nullable=True)
    instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    embodiment: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    mindwire_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_declared_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(f"source IN {AGENT_SESSION_SOURCES}", name="agent_sessions_source_check"),
        CheckConstraint(
            "source <> 'loop' OR mindwire_session_id IS NOT NULL",
            name="agent_sessions_loop_session_id_check",
        ),
        Index("idx_agent_sessions_identity", "identity"),
        Index("idx_agent_sessions_project", "project"),
    )
