"""SQLAlchemy ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Common DeclarativeBase for all chatroom tables."""


# Re-export concrete models so `Base.metadata` includes them when this
# package is imported (alembic env.py imports this for autogenerate).
from spirrow_conclair.models.event import ChatroomEvent  # noqa: E402, F401
from spirrow_conclair.models.message import Message  # noqa: E402, F401
from spirrow_conclair.models.project_control import (  # noqa: E402, F401
    ProjectControl,
    ProjectControlHistory,
)
from spirrow_conclair.models.read_cursor import ActorReadCursor  # noqa: E402, F401
from spirrow_conclair.models.thread import Thread  # noqa: E402, F401

__all__ = [
    "Base",
    "Thread",
    "Message",
    "ChatroomEvent",
    "ActorReadCursor",
    "ProjectControl",
    "ProjectControlHistory",
]
