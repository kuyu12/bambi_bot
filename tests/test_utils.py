from app.services.utils import chunk_text, clean_html_to_text, detect_dates, detect_prices


def test_clean_html_to_text_extracts_sections() -> None:
    html = "<html><head><title>X</title></head><body><h1>קורס מלגזה</h1><p>מחיר 1300 ש\"ח</p></body></html>"
    title, items = clean_html_to_text(html)
    assert title == "X"
    assert items[0][0] == "קורס מלגזה"
    assert "1300" in items[1][1]


def test_chunk_text_creates_chunks() -> None:
    chunks = chunk_text([(None, "a" * 900), (None, "b" * 900)], max_chars=1000)
    assert len(chunks) == 2


def test_detect_price_and_dates() -> None:
    text = "קורס מלגזה 1300 ש\"ח בתאריך 19.06.26"
    assert detect_prices(text)
    assert detect_dates(text) == ["19.06.26"]
