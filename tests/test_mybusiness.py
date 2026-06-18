from app.services.mybusiness import map_available_course, normalize_identifier_variants


def test_normalize_identifier_variants_for_israeli_mobile() -> None:
    assert normalize_identifier_variants("050-123-4567") == [
        "050-123-4567",
        "0501234567",
        "501234567",
        "972501234567",
        "+972501234567",
    ]


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
