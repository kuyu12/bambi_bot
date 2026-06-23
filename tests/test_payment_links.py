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


def test_hazmat_refresh_spelling_variants_match_refresh_not_course() -> None:
    service = PaymentLinkService()

    for query in ["רענון חומס", "ריענון חומס", "רענון הובלת חומס", "ריענון מוביל חומס"]:
        result = service.find_payment_instructions(query)

        assert result["found"] is True, query
        assert result["course"]["course_key"] == "hazmat_refresh"


def test_hazmat_transport_manager_refresh_is_distinct() -> None:
    service = PaymentLinkService()

    result = service.find_payment_instructions("ריענון אחראי שינוע חומס")

    assert result["found"] is True
    assert result["course"]["course_key"] == "hazmat_transport_manager_refresh"


def test_broad_hazmat_query_is_ambiguous() -> None:
    service = PaymentLinkService()

    result = service.find_payment_instructions("חומס")

    assert result["found"] is False
    assert result["ambiguous"] is True
    assert {match["course_key"] for match in result["matches"]} >= {
        "hazmat_refresh",
        "hazmat_transport_manager_refresh",
        "hazmat_course",
    }


def test_hazmat_transport_manager_course_does_not_use_refresh_link() -> None:
    service = PaymentLinkService()

    result = service.find_payment_instructions("קורס אחראי שינוע חומס")

    assert result["found"] is False
    assert result["matches_count"] == 0
