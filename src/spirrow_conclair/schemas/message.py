"""Message-related schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from spirrow_conclair.schemas.thread import Thread

MessageType = Literal[
    "propose", "question", "answer", "decide", "report", "handoff", "ack"
]


class Message(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    project: str
    msg_id: str
    thread_id: str
    author: str
    timestamp: datetime
    commit_ref: str | None = None
    type: MessageType
    content: str
    reply_to: str | None = None
    references_threads: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    closes_thread: str | None = None
    tags: list[str] = Field(default_factory=list)


class PostMessageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    type: MessageType
    author: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    reply_to: str | None = None
    references_threads: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    closes_thread: str | None = None
    tags: list[str] = Field(default_factory=list)
    commit_ref: str | None = None
    timestamp: datetime | None = None


class PostMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    msg: Message
    thread_status_changed_to: str | None = None


class CloseThreadRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    summary_content: str = Field(min_length=1)
    author: str = Field(min_length=1, max_length=200)
    affects_threads: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    commit_ref: str | None = None
    timestamp: datetime | None = None


class CloseThreadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    thread: Thread
    decide_msg: Message
