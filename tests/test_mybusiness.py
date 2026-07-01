from app.services.mybusiness import (
    course_search_keywords,
    map_available_course,
    match_categories,
    normalize_course_search,
    normalize_identifier_variants,
)


def test_normalize_identifier_variants_for_israeli_mobile() -> None:
    assert normalize_identifier_variants("050-123-4567") == [
        "050-123-4567",
        "0501234567",
        "501234567",
        "972501234567",
        "+972501234567",
    ]


def test_normalize_course_search_strips_common_prefixes() -> None:
    assert normalize_course_search("קורס מלגזה") == "מלגזה"
    assert normalize_course_search("רישיון מכונה ניידת") == "מכונה ניידת"


def test_course_keyword_matching_finds_long_category_from_partial_course_name() -> None:
    tachograph_course = "\u05d4\u05e9\u05ea\u05dc\u05de\u05d5\u05ea \u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9 \u05dc\u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea"
    safety_officer_course = "\u05e7\u05d5\u05e8\u05e1 \u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea \u05d1\u05ea\u05e2\u05d1\u05d5\u05e8\u05d4"
    query = "\u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9"
    categories = [
        {"category_id": "cat1", "name": tachograph_course, "code": ""},
        {"category_id": "cat2", "name": safety_officer_course, "code": ""},
    ]

    assert course_search_keywords(query) == ["\u05d8\u05db\u05d5\u05d2\u05e8\u05e3", "\u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9"]
    assert match_categories(categories, query) == [categories[0]]


def test_course_keyword_matching_does_not_match_single_weak_keyword() -> None:
    tachograph_course = "\u05d4\u05e9\u05ea\u05dc\u05de\u05d5\u05ea \u05d8\u05db\u05d5\u05d2\u05e8\u05e3 \u05d3\u05d9\u05d2\u05d9\u05d8\u05dc\u05d9 \u05dc\u05e7\u05e6\u05d9\u05e0\u05d9 \u05d1\u05d8\u05d9\u05d7\u05d5\u05ea"
    query = "\u05e7\u05e6\u05d9\u05e0\u05d9"
    categories = [{"category_id": "cat1", "name": tachograph_course, "code": ""}]

    assert match_categories(categories, query) == []


def test_map_available_course_skips_full_course() -> None:
    category = {"category_id": "cat1", "name": "מלגזה", "code": "80001"}
    row = {
        "objectId": "course1",
        "Name": "קורס מלגזה",
        "MaxCapacity": 10,
        "RegisteredStudents": 10,
    }

    assert map_available_course(row, category) is None


def test_map_available_course_returns_positive_capacity() -> None:
    category = {"category_id": "cat1", "name": "מלגזה", "code": "80001"}
    row = {
        "objectId": "course1",
        "Name": "קורס מלגזה",
        "StartDate": {"__type": "Date", "iso": "2026-06-19T09:00:00.000Z"},
        "MaxCapacity": 10,
        "RegisteredStudents": 7,
        "StatusId": {"objectId": "status1", "Name": "פתוח לרישום"},
    }

    course = map_available_course(row, category)

    assert course is not None
    assert course["available_seats"] == 3
    assert course["category_id"] == "cat1"
    assert course["status"] == "פתוח לרישום"
