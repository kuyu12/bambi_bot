from scripts.build_knowledge_tools import extract_page_text, html_to_text, normalize_tool_id


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
