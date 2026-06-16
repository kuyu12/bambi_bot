from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Citation(BaseModel):
    source_id: str
    title: str
    locator: str | None = None
    url: str | None = None
    snippet: str | None = None
    authority: Literal["primary", "secondary"] = "secondary"


class AgentAnswer(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]
    needs_human_review: bool = False
    follow_up_question: str | None = None


class ChatSessionCreateResponse(BaseModel):
    session_id: str
    created_at: datetime


class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatMessageResponse(BaseModel):
    session_id: str
    response: AgentAnswer
    created_at: datetime


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime


class ChatSessionDetail(BaseModel):
    session_id: str
    created_at: datetime
    updated_at: datetime
    history: list[ChatHistoryItem]


class SourceStatus(BaseModel):
    source_type: str
    total_sources: int
    total_chunks: int
    latest_success_at: datetime | None = None
    latest_error: str | None = None


class SourcesStatusResponse(BaseModel):
    statuses: list[SourceStatus]


class ReloadSourcesResponse(BaseModel):
    run_id: int
    status: str
    message: str


class ConflictRecord(BaseModel):
    id: int
    key: str
    field_name: str
    primary_value: str
    secondary_value: str
    status: str
    source_ids: list[str]
    created_at: datetime


class SourceChunk(BaseModel):
    source_id: str
    source_type: str
    title: str
    url: str | None = None
    section_heading: str | None = None
    content: str
    authority_rank: int
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    items: list[SourceChunk]


class CourseRecord(BaseModel):
    course_key: str
    course_name: str
    description: str | None = None
    price_text: str | None = None
    duration_text: str | None = None
    prerequisites_text: str | None = None
    source_id: str
    authority_rank: int = 50
    url: str | None = None


class SourceDocument(BaseModel):
    source_id: str
    source_type: str
    title: str
    content: str
    url: str | None = None
    updated_at: datetime | None = None
    authority_rank: int = 50
    metadata: dict[str, Any] = Field(default_factory=dict)
