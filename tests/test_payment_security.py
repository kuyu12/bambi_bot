from app.services.payment_links import PaymentLinkService


def test_allowed_payment_urls_contains_configured_payment_links() -> None:
    service = PaymentLinkService()

    allowed_urls = service.allowed_payment_urls()

    assert "https://tinyurl.com/3zdk77d6" in allowed_urls
    assert any("payment-btn-page" in url for url in allowed_urls)
