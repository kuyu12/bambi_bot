from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models


class VectorStore:
    def __init__(self, qdrant_url: str, collection_name: str, vector_size: int):
        self.client = QdrantClient(url=qdrant_url, check_compatibility=False)
        self.collection_name = collection_name
        self.vector_size = vector_size
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        collections = [item.name for item in self.client.get_collections().collections]
        if self.collection_name in collections:
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=models.VectorParams(size=self.vector_size, distance=models.Distance.COSINE),
        )

    def upsert(self, points: list[models.PointStruct]) -> None:
        if points:
            self.client.upsert(collection_name=self.collection_name, points=points)

    def delete_source(self, source_id: str) -> None:
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(must=[models.FieldCondition(key="source_id", match=models.MatchValue(value=source_id))])
            ),
        )

    def search(self, embedding: list[float], limit: int = 6, filters: dict[str, Any] | None = None) -> list[models.ScoredPoint]:
        query_filter = None
        if filters:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(key=key, match=models.MatchValue(value=value))
                    for key, value in filters.items()
                ]
            )
        return self.client.search(
            collection_name=self.collection_name,
            query_vector=embedding,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
