import json

from scripts.build_knowledge_tools import completion_key, extract_page_text, html_to_text, load_manifest, normalize_tool_id
from scripts.export_wordpress_raw import CHANGE_STATUS_CHANGED, CHANGE_STATUS_NEW, CHANGE_STATUS_UNCHANGED, change_status


def test_html_to_text_removes_noise() -> None:
    html = "<h1>כותרת</h1><script>alert(1)</script><p>תוכן&nbsp;חשוב</p><style>.x{}</style>"

    text = html_to_text(html)

    assert "כותרת" in text
    assert "תוכן חשוב" in text
    assert "alert" not in text
    assert ".x" not in text


def test_extract_page_text_uses_only_relevant_wordpress_fields() -> None:
    raw = {
        "title": {"rendered": "<strong>קורס מלגזה</strong>"},
        "content": {"rendered": "<div>משך הקורס: יומיים</div>"},
        "excerpt": {"rendered": "<p>תקציר</p>"},
        "yoast_head": "<script>noise</script>",
    }

    text = extract_page_text(raw)

    assert "קורס מלגזה" in text
    assert "משך הקורס: יומיים" in text
    assert "תקציר" in text
    assert "yoast" not in text


def test_normalize_tool_id() -> None:
    assert normalize_tool_id("Mobile Machine License!") == "mobile_machine_license"
    assert normalize_tool_id("123-course") == "tool_123_course"


def test_wordpress_delta_status_uses_hash_before_modified_date() -> None:
    assert change_status("new-hash", None, "2026-01-01T00:00:00") == CHANGE_STATUS_NEW
    assert (
        change_status(
            "same-hash",
            {"content_hash": "same-hash", "modified_gmt": "2025-01-01T00:00:00"},
            "2026-01-01T00:00:00",
        )
        == CHANGE_STATUS_UNCHANGED
    )
    assert (
        change_status(
            "new-hash",
            {"content_hash": "old-hash", "modified_gmt": "2026-01-01T00:00:00"},
            "2026-01-01T00:00:00",
        )
        == CHANGE_STATUS_CHANGED
    )
    assert (
        change_status(
            "new-hash",
            {"modified_gmt": "2026-01-01T00:00:00"},
            "2026-01-01T00:00:00",
        )
        == CHANGE_STATUS_UNCHANGED
    )


def test_load_manifest_can_load_only_changed_files(tmp_path) -> None:
    raw_dir = tmp_path / "raw-export"
    raw_dir.mkdir()
    files = [
        {"type": "pages", "id": 1, "file": "raw/pages/1.json"},
        {"type": "posts", "id": 2, "file": "raw/posts/2.json"},
    ]
    (raw_dir / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    (raw_dir / "changed_files.json").write_text(json.dumps({"files": files[:1]}), encoding="utf-8")

    assert load_manifest(raw_dir) == files
    assert load_manifest(raw_dir, only_changed=True) == files[:1]


def test_completion_key_can_include_content_hash_for_delta_resume() -> None:
    old_item = {"file": "raw/posts/1-course.json", "content_hash": "old"}
    new_item = {"file": "raw/posts/1-course.json", "content_hash": "new"}

    assert completion_key(old_item) == completion_key(new_item)
    assert completion_key(old_item, include_hash=True) != completion_key(new_item, include_hash=True)
