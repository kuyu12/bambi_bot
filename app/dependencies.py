from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.db import Database
from app.services.agent_service import AgentService
from app.services.knowledge_files import KnowledgeFileService


@lru_cache
def get_db() -> Database:
    settings = get_settings()
    db = Database(settings.sqlite_path)
    db.init_schema()
    return db


@lru_cache
def get_knowledge_file_service() -> KnowledgeFileService:
    return KnowledgeFileService()


@lru_cache
def get_agent_service() -> AgentService:
    return AgentService(get_settings(), get_db(), get_knowledge_file_service())
