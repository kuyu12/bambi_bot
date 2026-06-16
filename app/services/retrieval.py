from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.db import Database
from app.schemas import Citation, CourseRecord, SearchResult, SourceChunk
from app.services.vector_store import VectorStore


class RetrievalService:
    def __init__(self, settings: Settings, db: Database, vector_store: VectorStore):
        self.settings = settings
        self.db = db
        self.vector_store = vector_store
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def embed_query(self, text: str) -> list[float]:
        response = await self.client.embeddings.create(
            model=self.settings.openai_embedding_model,
            input=[text],
        )
        return response.data[0].embedding

    def search_courses(self, query: str) -> list[CourseRecord]:
        return [
            CourseRecord(
                course_key=row["course_key"],
                course_name=row["course_name"],
                description=row["description"],
                price_text=row["price_text"],
                duration_text=row["duration_text"],
                prerequisites_text=row["prerequisites_text"],
                source_id=row["source_id"],
                authority_rank=row["authority_rank"],
                url=row["url"],
            )
            for row in self.db.search_courses(query)
        ]

    def get_course_details(self, course_id_or_name: str) -> list[CourseRecord]:
        return [
            CourseRecord(
                course_key=row["course_key"],
                course_name=row["course_name"],
                description=row["description"],
                price_text=row["price_text"],
                duration_text=row["duration_text"],
                prerequisites_text=row["prerequisites_text"],
                source_id=row["source_id"],
                authority_rank=row["authority_rank"],
                url=row["url"],
            )
            for row in self.db.get_course_details(course_id_or_name)
        ]

    def get_course_dates(self, course_id_or_name: str, location: str | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.get_course_dates(course_id_or_name, location)]

    def search_faq(self, query: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.search_faq(query)]

    async def search_knowledge(self, query: str, source_types: list[str] | None = None) -> SearchResult:
        if not self.settings.openai_api_key:
            return SearchResult(items=[])
        filters = None
        if source_types and len(source_types) == 1:
            filters = {"source_type": source_types[0]}
        embedding = await self.embed_query(query)
        points = self.vector_store.search(embedding, limit=6, filters=filters)
        items = []
        for point in points:
            payload = point.payload or {}
            if source_types and payload.get("source_type") not in source_types:
                continue
            items.append(
                SourceChunk(
                    source_id=str(payload["source_id"]),
                    source_type=str(payload["source_type"]),
                    title=str(payload["title"]),
                    url=payload.get("url"),
                    section_heading=payload.get("section_heading"),
                    content=str(payload["content"]),
                    authority_rank=int(payload["authority_rank"]),
                    updated_at=None,
                    metadata={"score": point.score},
                )
            )
        return SearchResult(items=items)

    def get_source_record(self, source_id: str) -> dict[str, Any] | None:
        source = self.db.get_source(source_id)
        if not source:
            return None
        chunks = self.db.get_source_chunks(source_id)
        return {
            "source_id": source["source_id"],
            "source_type": source["source_type"],
            "title": source["title"],
            "url": source["url"],
            "updated_at": source["updated_at"],
            "authority_rank": source["authority_rank"],
            "metadata": json.loads(source["metadata_json"]),
            "content": source["raw_content"],
            "chunks": [dict(chunk) for chunk in chunks],
        }

    def build_citations_for_courses(self, courses: list[CourseRecord]) -> list[Citation]:
        items = []
        for course in courses[:3]:
            items.append(
                Citation(
                    source_id=course.source_id,
                    title=course.course_name,
                    locator=course.description[:120] if course.description else None,
                    url=course.url,
                    snippet=course.price_text or course.prerequisites_text or course.duration_text,
                    authority="primary" if course.authority_rank <= 20 else "secondary",
                )
            )
        return items
