from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pypdf import PdfReader
from qdrant_client.http import models

from app.config import Settings
from app.db import Database
from app.services.utils import (
    chunk_text,
    clean_html_to_text,
    collect_internal_links,
    detect_dates,
    detect_prices,
    normalize_whitespace,
    sha256_text,
    slugify_hebrew_fallback,
)
from app.services.vector_store import VectorStore


COURSE_HINT_RE = re.compile(r"קורס|רישיון|היתר|הכשרה|השתלמות|מלגזה|טרקטור|משא|עגורן|בטיחות", re.UNICODE)
logger = logging.getLogger(__name__)


@dataclass
class SourcePayload:
    source_id: str
    source_type: str
    title: str
    url: str | None
    raw_content: str
    authority_rank: int
    updated_at: str | None
    metadata: dict[str, Any]
    chunks: list[dict[str, Any]]
    courses: list[dict[str, Any]]
    dates: list[dict[str, Any]]
    faqs: list[dict[str, Any]]
    links: list[dict[str, Any]]


class IngestionService:
    def __init__(self, settings: Settings, db: Database, vector_store: VectorStore):
        self.settings = settings
        self.db = db
        self.vector_store = vector_store

    async def ingest_all(self) -> int:
        run_id = self.db.start_ingestion_run("all")
        stats: dict[str, Any] = {"website_sources": 0, "google_sources": 0}
        try:
            website_count = await self.ingest_website()
            google_count = await self.ingest_google_sources()
            stats["website_sources"] = website_count
            stats["google_sources"] = google_count
            self.db.finish_ingestion_run(run_id, "success", stats=stats)
        except Exception as exc:  # pragma: no cover - startup fallback
            self.db.finish_ingestion_run(run_id, "failed", stats=stats, error=str(exc))
            raise
        return run_id

    async def ingest_website(self) -> int:
        run_id = self.db.start_ingestion_run("website")
        count = 0
        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            ) as client:
                payloads = await self._crawl_website(client)
                for payload in payloads:
                    await self._store_payload(payload)
                    count += 1
            self.db.finish_ingestion_run(run_id, "success", stats={"sources": count})
            return count
        except Exception as exc:
            self.db.finish_ingestion_run(run_id, "failed", stats={"sources": count}, error=str(exc))
            raise

    async def ingest_google_sources(self) -> int:
        run_id = self.db.start_ingestion_run("google")
        count = 0
        try:
            if self.settings.google_shared_doc_export_url:
                payload = await self._ingest_shared_doc()
                await self._store_payload(payload)
                count += 1
            for payload in await self._ingest_drive_sources():
                await self._store_payload(payload)
                count += 1
            self.db.finish_ingestion_run(run_id, "success", stats={"sources": count})
            return count
        except Exception as exc:
            self.db.finish_ingestion_run(run_id, "failed", stats={"sources": count}, error=str(exc))
            raise

    async def _crawl_website(self, client: httpx.AsyncClient) -> list[SourcePayload]:
        queue = deque([self.settings.website_base_url])
        seen: set[str] = set()
        payloads: list[SourcePayload] = []

        while queue and len(seen) < self.settings.website_max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("Website fetch failed for %s: %s", url, exc)
                continue
            if response.status_code >= 400:
                logger.warning("Website fetch blocked for %s with status %s", url, response.status_code)
                continue
            html = response.text
            title, items = clean_html_to_text(html)
            chunks = chunk_text(items)
            raw_content = "\n".join(chunk["content"] for chunk in chunks)
            payloads.append(
                self._normalize_source(
                    source_id=f"website:{url}",
                    source_type="website",
                    title=title or urlparse(url).path.strip("/") or "Bambi website",
                    url=url,
                    raw_content=raw_content,
                    authority_rank=10,
                    updated_at=self._http_last_modified(response),
                    chunks=chunks,
                    metadata={"http_headers": dict(response.headers)},
                )
            )
            for link in collect_internal_links(html, url, self.settings.website_allowed_host):
                if link not in seen:
                    queue.append(link)
        return payloads

    async def _ingest_shared_doc(self) -> SourcePayload:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(self.settings.google_shared_doc_export_url)
            response.raise_for_status()
            text = response.text
        lines = [normalize_whitespace(line) for line in text.splitlines() if normalize_whitespace(line)]
        title = lines[0] if lines else "Shared course doc"
        items = []
        current_heading: str | None = title
        for line in lines:
            if line.endswith(":") or len(line) < 80:
                current_heading = line
            items.append((current_heading, line))
        return self._normalize_source(
            source_id=self.settings.google_shared_doc_source_id,
            source_type="google_doc",
            title=title,
            url=self.settings.google_shared_doc_export_url,
            raw_content="\n".join(lines),
            authority_rank=30,
            updated_at=datetime.now(UTC).isoformat(),
            chunks=chunk_text(items),
            metadata={"shared_export": True},
        )

    async def _ingest_drive_sources(self) -> list[SourcePayload]:
        if not self.settings.google_service_account_json:
            return []

        credentials = service_account.Credentials.from_service_account_file(
            self.settings.google_service_account_json,
            scopes=["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/documents.readonly"],
        )
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)

        file_ids = set(self.settings.drive_file_ids)
        for folder_id in self.settings.drive_folder_ids:
            query = f"'{folder_id}' in parents and trashed = false"
            response = drive.files().list(q=query, fields="files(id,name,mimeType,modifiedTime,webViewLink)").execute()
            for item in response.get("files", []):
                file_ids.add(item["id"])

        payloads: list[SourcePayload] = []
        for file_id in sorted(file_ids):
            meta = drive.files().get(fileId=file_id, fields="id,name,mimeType,modifiedTime,webViewLink").execute()
            mime_type = meta["mimeType"]
            if mime_type == "application/vnd.google-apps.document":
                payloads.append(self._normalize_google_doc(file_id, meta, docs))
            elif mime_type == "application/vnd.google-apps.spreadsheet":
                payloads.append(self._normalize_google_sheet(file_id, meta, sheets))
            elif mime_type == "application/pdf":
                payloads.append(self._normalize_pdf(file_id, meta, drive))
        return payloads

    def _normalize_google_doc(self, file_id: str, meta: dict[str, Any], docs_service: Any) -> SourcePayload:
        doc = docs_service.documents().get(documentId=file_id).execute()
        lines: list[str] = []
        for element in doc.get("body", {}).get("content", []):
            paragraph = element.get("paragraph")
            if not paragraph:
                continue
            text_runs = []
            for item in paragraph.get("elements", []):
                text = item.get("textRun", {}).get("content")
                if text:
                    text_runs.append(text)
            line = normalize_whitespace("".join(text_runs))
            if line:
                lines.append(line)
        items = [(None, line) for line in lines]
        return self._normalize_source(
            source_id=f"drive:{file_id}",
            source_type="google_doc",
            title=meta["name"],
            url=meta.get("webViewLink"),
            raw_content="\n".join(lines),
            authority_rank=35,
            updated_at=meta.get("modifiedTime"),
            chunks=chunk_text(items),
            metadata={"file_id": file_id, "mime_type": meta["mimeType"]},
        )

    def _normalize_google_sheet(self, file_id: str, meta: dict[str, Any], sheets_service: Any) -> SourcePayload:
        spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=file_id, includeGridData=False).execute()
        items: list[tuple[str | None, str]] = []
        rows: list[str] = []
        for sheet in spreadsheet.get("sheets", []):
            title = sheet["properties"]["title"]
            values = sheets_service.spreadsheets().values().get(spreadsheetId=file_id, range=title).execute().get("values", [])
            for row in values:
                row_text = " | ".join(str(cell) for cell in row)
                rows.append(row_text)
                items.append((title, row_text))
        return self._normalize_source(
            source_id=f"drive:{file_id}",
            source_type="google_sheet",
            title=meta["name"],
            url=meta.get("webViewLink"),
            raw_content="\n".join(rows),
            authority_rank=35,
            updated_at=meta.get("modifiedTime"),
            chunks=chunk_text(items or [(None, meta["name"])]),
            metadata={"file_id": file_id, "mime_type": meta["mimeType"]},
        )

    def _normalize_pdf(self, file_id: str, meta: dict[str, Any], drive_service: Any) -> SourcePayload:
        request = drive_service.files().get_media(fileId=file_id)
        import io

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buffer.seek(0)
        reader = PdfReader(buffer)
        lines = [normalize_whitespace(page.extract_text() or "") for page in reader.pages]
        lines = [line for line in lines if line]
        items = [(f"Page {i+1}", line) for i, line in enumerate(lines)]
        return self._normalize_source(
            source_id=f"drive:{file_id}",
            source_type="pdf",
            title=meta["name"],
            url=meta.get("webViewLink"),
            raw_content="\n".join(lines),
            authority_rank=40,
            updated_at=meta.get("modifiedTime"),
            chunks=chunk_text(items or [(None, meta["name"])]),
            metadata={"file_id": file_id, "mime_type": meta["mimeType"]},
        )

    def _normalize_source(
        self,
        *,
        source_id: str,
        source_type: str,
        title: str,
        url: str | None,
        raw_content: str,
        authority_rank: int,
        updated_at: str | None,
        chunks: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> SourcePayload:
        course_name = title if COURSE_HINT_RE.search(title) else None
        courses: list[dict[str, Any]] = []
        dates: list[dict[str, Any]] = []
        faqs: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []

        if course_name or any(COURSE_HINT_RE.search(chunk["content"]) for chunk in chunks):
            key = slugify_hebrew_fallback(course_name or title)
            prices = detect_prices(raw_content)
            found_dates = detect_dates(raw_content)
            courses.append(
                {
                    "course_key": key,
                    "course_name": course_name or title,
                    "description": chunks[0]["content"][:1000] if chunks else raw_content[:1000],
                    "price_text": prices[0] if prices else None,
                    "duration_text": self._first_matching_line(raw_content, ["שעות", "מפגשים", "ימים", "חודש"]),
                    "prerequisites_text": self._first_matching_line(raw_content, ["תנאי", "דרישות", "גיל", "רישיון"]),
                    "url": url,
                    "authority_rank": authority_rank,
                    "metadata": metadata,
                }
            )
            for item in found_dates[:12]:
                dates.append({"course_key": key, "date_text": item, "authority_rank": authority_rank, "metadata": metadata})

        for line in raw_content.splitlines():
            if "?" in line and len(line) < 180:
                faqs.append(
                    {
                        "question": line,
                        "answer": self._next_line_after(raw_content, line),
                        "category": "faq",
                        "authority_rank": authority_rank,
                        "metadata": metadata,
                    }
                )

        for token in re.findall(r"https?://\S+", raw_content):
            normalized = token.rstrip(").,")
            link_type = "registration_link"
            if "payment" in normalized or "mybooks" in normalized:
                link_type = "payment_link"
            links.append(
                {
                    "course_key": slugify_hebrew_fallback(course_name or title),
                    "link_type": link_type,
                    "label": title,
                    "url": normalized,
                    "authority_rank": authority_rank,
                    "metadata": metadata,
                }
            )

        return SourcePayload(
            source_id=source_id,
            source_type=source_type,
            title=title,
            url=url,
            raw_content=raw_content,
            authority_rank=authority_rank,
            updated_at=updated_at,
            metadata=metadata,
            chunks=chunks,
            courses=courses,
            dates=dates,
            faqs=faqs,
            links=links,
        )

    async def _store_payload(self, payload: SourcePayload) -> None:
        revision_hash = sha256_text(payload.raw_content)
        self.db.replace_source(
            source_id=payload.source_id,
            source_type=payload.source_type,
            title=payload.title,
            url=payload.url,
            revision_hash=revision_hash,
            updated_at=payload.updated_at,
            authority_rank=payload.authority_rank,
            raw_content=payload.raw_content,
            metadata=payload.metadata,
            chunks=payload.chunks,
        )
        self.db.replace_course_records(
            payload.source_id,
            payload.courses,
            payload.dates,
            payload.faqs,
            payload.links,
        )
        if self.settings.openai_api_key:
            self.vector_store.delete_source(payload.source_id)
            points: list[models.PointStruct] = []
            embeddings = await self._embed_batch([chunk["content"] for chunk in payload.chunks])
            for idx, (chunk, embedding) in enumerate(zip(payload.chunks, embeddings, strict=False)):
                points.append(
                    models.PointStruct(
                        id=sha256_text(f"{payload.source_id}:{idx}")[:32],
                        vector=embedding,
                        payload={
                            "source_id": payload.source_id,
                            "source_type": payload.source_type,
                            "title": payload.title,
                            "url": payload.url,
                            "section_heading": chunk.get("section_heading"),
                            "content": chunk["content"],
                            "authority_rank": payload.authority_rank,
                            "updated_at": payload.updated_at,
                        },
                    )
                )
            self.vector_store.upsert(points)
        self._detect_conflicts(payload)

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        from openai import AsyncOpenAI

        if not texts:
            return []
        client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        response = await client.embeddings.create(model=self.settings.openai_embedding_model, input=texts)
        return [item.embedding for item in response.data]

    def _detect_conflicts(self, payload: SourcePayload) -> None:
        if payload.source_type == "website":
            return
        for record in payload.courses:
            website_records = self.db.get_course_details(record["course_name"])
            for website_record in website_records:
                if website_record["authority_rank"] > 20:
                    continue
                for field_name in ("price_text", "prerequisites_text", "duration_text"):
                    secondary = record.get(field_name) or ""
                    primary = website_record[field_name] or ""
                    if secondary and primary and secondary != primary:
                        self.db.record_conflict(
                            key=record["course_key"],
                            field_name=field_name,
                            primary_value=primary,
                            secondary_value=secondary,
                            primary_source_id=website_record["source_id"],
                            secondary_source_id=payload.source_id,
                        )

    @staticmethod
    def _http_last_modified(response: httpx.Response) -> str | None:
        header = response.headers.get("last-modified")
        if not header:
            return None
        return header

    @staticmethod
    def _first_matching_line(text: str, terms: list[str]) -> str | None:
        for line in text.splitlines():
            if any(term in line for term in terms):
                return line[:500]
        return None

    @staticmethod
    def _next_line_after(text: str, needle: str) -> str:
        lines = [line for line in text.splitlines() if line]
        for idx, line in enumerate(lines):
            if line == needle and idx + 1 < len(lines):
                return lines[idx + 1]
        return ""
