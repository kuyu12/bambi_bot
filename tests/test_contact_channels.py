from app.services.contact_channels import ContactChannelService


def test_forklift_uses_dedicated_forklift_whatsapp() -> None:
    service = ContactChannelService()

    result = service.find_course_contact("קורס מלגזה")

    assert result["found"] is True
    assert result["contact"]["owner"] == "מרינה"
    assert result["contact"]["phone"] == "054-968-8028"


def test_forklift_instructor_uses_instructor_family_not_general_forklift() -> None:
    service = ContactChannelService()

    result = service.find_course_contact("ריענון מדריך מלגזה")

    assert result["found"] is True
    assert result["contact"]["phone"] == "054-580-6131"


def test_hazmat_and_public_transport_use_marina_transport_whatsapp() -> None:
    service = ContactChannelService()

    for course_name in ["ריענון חומס", "ריענון אחראי שינוע חומס", "קורס רכב ציבורי"]:
        result = service.find_course_contact(course_name)

        assert result["found"] is True, course_name
        assert result["contact"]["phone"] == "054-580-6131"


def test_work_at_height_and_cranes_use_hen_whatsapp() -> None:
    service = ContactChannelService()

    for course_name in ["קורס עבודה בגובה", "קורס עגורן גשר", "חידוש רישיון מנוף"]:
        result = service.find_course_contact(course_name)

        assert result["found"] is True, course_name
        assert result["contact"]["phone"] == "054-904-7872"


def test_tractor_mobile_machine_and_heavy_vehicle_use_yarin_whatsapp() -> None:
    service = ContactChannelService()

    for course_name in ["קורס טרקטור", "קורס מכונה ניידת", "קורס משא כבד"]:
        result = service.find_course_contact(course_name)

        assert result["found"] is True, course_name
        assert result["contact"]["phone"] == "054-904-7652"


def test_unknown_course_falls_back_to_office_contact() -> None:
    service = ContactChannelService()

    result = service.find_course_contact("קורס לא ידוע")

    assert result["found"] is False
    assert result["fallback"]["phone"] == "074-70-87-030"
