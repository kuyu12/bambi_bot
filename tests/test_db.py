from pathlib import Path

from app.db import Database


def test_database_session_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.init_schema()
    db.upsert_session("s1")
    db.add_message("s1", "user", "hello")
    session = db.get_session("s1")
    messages = db.get_messages("s1")
    assert session is not None
    assert len(messages) == 1
