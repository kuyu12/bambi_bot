from app.services.payment_links import is_approved_dynamic_payment_url


def test_allows_dynamic_mbapps_payment_url() -> None:
    assert (
        is_approved_dynamic_payment_url(
            "https://6a09b3ab-e66c-64a7-7dbc-06c797b56505.mbapps.co.il/apps/mybooks/payment-btn-page?cls=PaymentBtns&oid=abc123"
        )
        is True
    )


def test_blocks_unapproved_payment_url() -> None:
    assert (
        is_approved_dynamic_payment_url("https://evil.example.com/apps/mybooks/payment-btn-page?cls=PaymentBtns&oid=abc123")
        is False
    )
