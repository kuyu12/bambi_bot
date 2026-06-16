from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES chat_sessions(session_id)
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    revision_hash TEXT NOT NULL,
    updated_at TEXT,
    authority_rank INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    raw_content TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    section_heading TEXT,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_id, chunk_index),
    FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_key TEXT NOT NULL,
    course_name TEXT NOT NULL,
    description TEXT,
    price_text TEXT,
    duration_text TEXT,
    prerequisites_text TEXT,
    source_id TEXT NOT NULL,
    authority_rank INTEGER NOT NULL,
    url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(course_key, source_id),
    FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS course_dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_key TEXT NOT NULL,
    date_text TEXT NOT NULL,
    location TEXT,
    source_id TEXT NOT NULL,
    authority_rank INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(course_key, date_text, source_id),
    FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS faq_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    category TEXT,
    source_id TEXT NOT NULL,
    authority_rank INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS course_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_key TEXT NOT NULL,
    link_type TEXT NOT NULL,
    label TEXT,
    url TEXT NOT NULL,
    source_id TEXT NOT NULL,
    authority_rank INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(course_key, link_type, url, source_id),
    FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE TABLE IF NOT EXISTS source_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    primary_value TEXT NOT NULL,
    secondary_value TEXT NOT NULL,
    primary_source_id TEXT NOT NULL,
    secondary_source_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    UNIQUE(key, field_name, primary_source_id, secondary_source_id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    success INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)

    def upsert_session(self, session_id: str) -> None:
        now = utcnow()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions(session_id, created_at, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (session_id, now, now),
            )

    def add_message(self, session_id: str, role: str, content: str) -> None:
        now = utcnow()
        self.upsert_session(session_id)
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO chat_messages(session_id, role, content, created_at) VALUES(?, ?, ?, ?)",
                (session_id, role, content, now),
            )
            conn.execute("UPDATE chat_sessions SET updated_at=? WHERE session_id=?", (now, session_id))

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT session_id, created_at, updated_at FROM chat_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()

    def get_messages(self, session_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                "SELECT role, content, created_at FROM chat_messages WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()

    def start_ingestion_run(self, source_type: str) -> int:
        with self.connection() as conn:
            cur = conn.execute(
                "INSERT INTO ingestion_runs(source_type, status, started_at) VALUES(?, 'running', ?)",
                (source_type, utcnow()),
            )
            return int(cur.lastrowid)

    def finish_ingestion_run(self, run_id: int, status: str, stats: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE ingestion_runs SET status=?, finished_at=?, stats_json=?, error=? WHERE id=?",
                (status, utcnow(), json.dumps(stats or {}), error, run_id),
            )

    def replace_source(
        self,
        *,
        source_id: str,
        source_type: str,
        title: str,
        url: str | None,
        revision_hash: str,
        updated_at: str | None,
        authority_rank: int,
        raw_content: str,
        metadata: dict[str, Any],
        chunks: list[dict[str, Any]],
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO sources(source_id, source_type, title, url, revision_hash, updated_at, authority_rank, metadata_json, raw_content)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_type=excluded.source_type,
                    title=excluded.title,
                    url=excluded.url,
                    revision_hash=excluded.revision_hash,
                    updated_at=excluded.updated_at,
                    authority_rank=excluded.authority_rank,
                    metadata_json=excluded.metadata_json,
                    raw_content=excluded.raw_content
                """,
                (source_id, source_type, title, url, revision_hash, updated_at, authority_rank, json.dumps(metadata), raw_content),
            )
            conn.execute("DELETE FROM source_chunks WHERE source_id=?", (source_id,))
            for idx, chunk in enumerate(chunks):
                conn.execute(
                    """
                    INSERT INTO source_chunks(source_id, chunk_index, section_heading, content, metadata_json)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        idx,
                        chunk.get("section_heading"),
                        chunk["content"],
                        json.dumps(chunk.get("metadata", {})),
                    ),
                )

    def replace_course_records(
        self,
        source_id: str,
        courses: list[dict[str, Any]],
        dates: list[dict[str, Any]],
        faqs: list[dict[str, Any]],
        links: list[dict[str, Any]],
    ) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM courses WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM course_dates WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM faq_entries WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM course_links WHERE source_id=?", (source_id,))

            seen_courses: set[tuple[str, str]] = set()
            for course in courses:
                course_key = (course["course_key"], course.get("course_name", ""))
                if course_key in seen_courses:
                    continue
                seen_courses.add(course_key)
                conn.execute(
                    """
                    INSERT INTO courses(course_key, course_name, description, price_text, duration_text, prerequisites_text, source_id, authority_rank, url, metadata_json)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        course["course_key"],
                        course["course_name"],
                        course.get("description"),
                        course.get("price_text"),
                        course.get("duration_text"),
                        course.get("prerequisites_text"),
                        source_id,
                        course.get("authority_rank", 50),
                        course.get("url"),
                        json.dumps(course.get("metadata", {})),
                    ),
                )
            seen_dates: set[tuple[str, str, str | None]] = set()
            for item in dates:
                date_key = (item["course_key"], item["date_text"], item.get("location"))
                if date_key in seen_dates:
                    continue
                seen_dates.add(date_key)
                conn.execute(
                    """
                    INSERT INTO course_dates(course_key, date_text, location, source_id, authority_rank, metadata_json)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["course_key"],
                        item["date_text"],
                        item.get("location"),
                        source_id,
                        item.get("authority_rank", 50),
                        json.dumps(item.get("metadata", {})),
                    ),
                )
            seen_faqs: set[tuple[str, str]] = set()
            for item in faqs:
                faq_key = (item["question"], item["answer"])
                if faq_key in seen_faqs:
                    continue
                seen_faqs.add(faq_key)
                conn.execute(
                    """
                    INSERT INTO faq_entries(question, answer, category, source_id, authority_rank, metadata_json)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["question"],
                        item["answer"],
                        item.get("category"),
                        source_id,
                        item.get("authority_rank", 50),
                        json.dumps(item.get("metadata", {})),
                    ),
                )
            seen_links: set[tuple[str, str, str]] = set()
            for item in links:
                link_key = (item["course_key"], item["link_type"], item["url"])
                if link_key in seen_links:
                    continue
                seen_links.add(link_key)
                conn.execute(
                    """
                    INSERT INTO course_links(course_key, link_type, label, url, source_id, authority_rank, metadata_json)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["course_key"],
                        item["link_type"],
                        item.get("label"),
                        item["url"],
                        source_id,
                        item.get("authority_rank", 50),
                        json.dumps(item.get("metadata", {})),
                    ),
                )

    def record_conflict(
        self,
        *,
        key: str,
        field_name: str,
        primary_value: str,
        secondary_value: str,
        primary_source_id: str,
        secondary_source_id: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO source_conflicts(
                    key, field_name, primary_value, secondary_value, primary_source_id, secondary_source_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, field_name, primary_value, secondary_value, primary_source_id, secondary_source_id, utcnow()),
            )

    def get_conflicts(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT id, key, field_name, primary_value, secondary_value, primary_source_id, secondary_source_id, status, created_at
                FROM source_conflicts
                ORDER BY id DESC
                """
            ).fetchall()

    def log_tool_call(self, session_id: str | None, tool_name: str, tool_input: dict[str, Any], tool_output: dict[str, Any], success: bool, error: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO tool_calls(session_id, tool_name, input_json, output_json, success, error, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, tool_name, json.dumps(tool_input), json.dumps(tool_output), int(success), error, utcnow()),
            )

    def source_statuses(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT
                    s.source_type,
                    COUNT(DISTINCT s.source_id) AS total_sources,
                    COUNT(sc.id) AS total_chunks,
                    (
                      SELECT ir.finished_at FROM ingestion_runs ir
                      WHERE ir.source_type = s.source_type AND ir.status = 'success'
                      ORDER BY ir.id DESC LIMIT 1
                    ) AS latest_success_at,
                    (
                      SELECT ir.error FROM ingestion_runs ir
                      WHERE ir.source_type = s.source_type AND ir.status = 'failed'
                      ORDER BY ir.id DESC LIMIT 1
                    ) AS latest_error
                FROM sources s
                LEFT JOIN source_chunks sc ON sc.source_id = s.source_id
                GROUP BY s.source_type
                ORDER BY s.source_type
                """
            ).fetchall()

    def search_courses(self, query: str) -> list[sqlite3.Row]:
        like = f"%{query}%"
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM courses
                WHERE course_name LIKE ? OR description LIKE ? OR prerequisites_text LIKE ?
                ORDER BY authority_rank ASC, course_name ASC
                LIMIT 10
                """,
                (like, like, like),
            ).fetchall()

    def get_course_details(self, course_id_or_name: str) -> list[sqlite3.Row]:
        like = f"%{course_id_or_name}%"
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM courses
                WHERE course_key = ? OR course_name LIKE ?
                ORDER BY authority_rank ASC, course_name ASC
                """,
                (course_id_or_name, like),
            ).fetchall()

    def get_course_dates(self, course_id_or_name: str, location: str | None = None) -> list[sqlite3.Row]:
        like = f"%{course_id_or_name}%"
        params: list[Any] = [course_id_or_name, like]
        location_sql = ""
        if location:
            location_sql = " AND COALESCE(location, '') LIKE ?"
            params.append(f"%{location}%")
        with self.connection() as conn:
            return conn.execute(
                f"""
                SELECT cd.* FROM course_dates cd
                WHERE cd.course_key = ? OR cd.course_key LIKE ? {location_sql}
                ORDER BY cd.authority_rank ASC, cd.date_text ASC
                """,
                params,
            ).fetchall()

    def search_faq(self, query: str) -> list[sqlite3.Row]:
        like = f"%{query}%"
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT * FROM faq_entries
                WHERE question LIKE ? OR answer LIKE ? OR category LIKE ?
                ORDER BY authority_rank ASC, question ASC
                LIMIT 10
                """,
                (like, like, like),
            ).fetchall()

    def get_source(self, source_id: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()

    def get_source_chunks(self, source_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM source_chunks WHERE source_id=? ORDER BY chunk_index",
                (source_id,),
            ).fetchall()
