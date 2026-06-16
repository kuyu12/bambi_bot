from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="Bambi Knowledge Agent", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    admin_api_token: str = Field(default="change-me", alias="ADMIN_API_TOKEN")

    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    sqlite_path: Path = Field(default=Path("./data/bambi.db"), alias="SQLITE_PATH")
    session_db_path: Path = Field(default=Path("./data/agent_sessions.db"), alias="SESSION_DB_PATH")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.5", alias="OPENAI_MODEL")
    openai_reasoning_effort: str = Field(default="low", alias="OPENAI_REASONING_EFFORT")
    openai_text_verbosity: str = Field(default="low", alias="OPENAI_TEXT_VERBOSITY")
    openai_embedding_model: str = Field(default="text-embedding-3-small", alias="OPENAI_EMBEDDING_MODEL")

    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_collection: str = Field(default="bambi-knowledge", alias="QDRANT_COLLECTION")
    qdrant_vector_size: int = Field(default=1536, alias="QDRANT_VECTOR_SIZE")

    website_base_url: str = Field(default="https://bambischool.co.il/", alias="WEBSITE_BASE_URL")
    website_allowed_host: str = Field(default="bambischool.co.il", alias="WEBSITE_ALLOWED_HOST")
    website_max_pages: int = Field(default=60, alias="WEBSITE_MAX_PAGES")

    google_service_account_json: str = Field(default="", alias="GOOGLE_SERVICE_ACCOUNT_JSON")
    google_drive_folder_ids: str = Field(default="", alias="GOOGLE_DRIVE_FOLDER_IDS")
    google_drive_file_ids: str = Field(default="", alias="GOOGLE_DRIVE_FILE_IDS")
    google_shared_doc_export_url: str = Field(default="", alias="GOOGLE_SHARED_DOC_EXPORT_URL")
    google_shared_doc_source_id: str = Field(default="shared-course-doc", alias="GOOGLE_SHARED_DOC_SOURCE_ID")

    ingest_on_startup: bool = Field(default=False, alias="INGEST_ON_STARTUP")

    def model_post_init(self, __context: object) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def drive_folder_ids(self) -> list[str]:
        return [item.strip() for item in self.google_drive_folder_ids.split(",") if item.strip()]

    @property
    def drive_file_ids(self) -> list[str]:
        return [item.strip() for item in self.google_drive_file_ids.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
