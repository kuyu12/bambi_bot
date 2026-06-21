from app.services.mybusiness import map_customer


def test_map_customer_masks_sensitive_fields() -> None:
    customer = map_customer(
        {
            "objectId": "account1",
            "Name": "Test Customer",
            "Email": "student@example.com",
            "PhoneNumber": "050-123-4567",
            "StudentId": "123456789",
            "IdClient": "987654321",
            "CompanyId": "514857556",
        }
    )

    assert customer["account_id"] == "account1"
    assert customer["email"] == "st***@example.com"
    assert customer["phone_number"] == "***4567"
    assert customer["student_id"] == "***6789"
    assert customer["id_client"] == "***4321"
    assert customer["company_id"] == "***7556"
