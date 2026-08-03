"""Pydantic request/response schemas.

Naming convention (per docs/api-design.md §4):
- `Thread`, `Message`, `ChatroomEvent`: entity-shaped DTOs (no suffix)
- `*Request`: request body
- `*Response`: response body
- `*ListResponse`: paginated list responses
"""

from spirrow_conclair.schemas.event import (
    ChatroomEvent,
    EventListResponse,
    IntegrityCheckResponse,
    IntegrityIssue,
)
from spirrow_conclair.schemas.message import (
    CloseThreadRequest,
    CloseThreadResponse,
    Message,
    MessageType,
    PostMessageRequest,
    PostMessageResponse,
)
from spirrow_conclair.schemas.project import (
    ProjectSummary,
    ProjectSummaryListResponse,
)
from spirrow_conclair.schemas.read_cursor import (
    MarkReadRequest,
    MarkReadResponse,
    UnreadListResponse,
    UnreadThreadItem,
)
from spirrow_conclair.schemas.thread import (
    OpenThreadRequest,
    OpenThreadResponse,
    Thread,
    ThreadListResponse,
    ThreadStatus,
    ThreadView,
)

# Forward references for Message inside Thread schemas resolve once the
# Message symbol is in scope.
OpenThreadResponse.model_rebuild(_types_namespace={"Message": Message})
ThreadView.model_rebuild(_types_namespace={"Message": Message})

__all__ = [
    "ChatroomEvent",
    "CloseThreadRequest",
    "CloseThreadResponse",
    "EventListResponse",
    "IntegrityCheckResponse",
    "IntegrityIssue",
    "MarkReadRequest",
    "MarkReadResponse",
    "Message",
    "MessageType",
    "OpenThreadRequest",
    "OpenThreadResponse",
    "PostMessageRequest",
    "PostMessageResponse",
    "ProjectSummary",
    "ProjectSummaryListResponse",
    "Thread",
    "ThreadListResponse",
    "ThreadStatus",
    "ThreadView",
    "UnreadListResponse",
    "UnreadThreadItem",
]
