from app.services.payment_links import PaymentLinkService


def test_find_payment_instructions_by_course_name() -> None:
    service = PaymentLinkService()

    result = service.find_payment_instructions("קורס מלגזה")

    assert result["found"] is True
    assert result["course"]["course_key"] == "forklift_course"
    assert result["course"]["payment_link"].startswith("https://")
    assert "שם מלא" in result["course"]["required_customer_details"]


def test_find_payment_instructions_by_alias() -> None:
    service = PaymentLinkService()

    result = service.find_payment_instructions("רשיון טרקטור")

    assert result["found"] is True
    assert result["course"]["course_key"] == "tractor_course"
    assert result["course"]["bank_transfer"]["beneficiary"] == "מכללת במבי"


def test_find_payment_instructions_returns_missing_for_unconfigured_course() -> None:
    service = PaymentLinkService()

    result = service.find_payment_instructions("קורס לא קיים")

    assert result["found"] is False
    assert result["matches_count"] == 0
