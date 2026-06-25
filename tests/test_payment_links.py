import asyncio
import json
from typing import Any

from app.services.mybusiness import pointer
from app.services.payment_links import PaymentLinkService, REQUIRED_CUSTOMER_DETAILS


class FakeMyBusiness:
    is_configured = True

    def __init__(self) -> None:
        self.categories = [
            {"objectId": "cat_forklift", "Name": "מלגזה", "Code": "80001"},
            {"objectId": "cat_forklift_refresh", "Name": "רענון מלגזה", "Code": "80003"},
            {"objectId": "cat_tractor", "Name": "טרקטור", "Code": "80007"},
            {"objectId": "cat_discount_only", "Name": "קורס הנחות", "Code": "99999"},
        ]
        self.products = [
            {
                "objectId": "prod_forklift",
                "Name": "קורס מלגזה",
                "CatalogNumber": "80001",
                "Price": 1102,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_forklift"),
            },
            {
                "objectId": "prod_forklift_friday",
                "Name": "קורס מלגזה ימי שישי",
                "CatalogNumber": "80001-F",
                "Price": 1186,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_forklift"),
            },
            {
                "objectId": "prod_forklift_refresh",
                "Name": "רענון מלגזה",
                "CatalogNumber": "80003",
                "Price": 339,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_forklift_refresh"),
            },
            {
                "objectId": "prod_tractor",
                "Name": "קורס טרקטור",
                "CatalogNumber": "80007",
                "Price": 2712,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_tractor"),
            },
            {
                "objectId": "prod_discount_only",
                "Name": "קורס הנחות",
                "CatalogNumber": "99999",
                "Price": 100,
                "IsActive": True,
                "Category": pointer("ProductCategories", "cat_discount_only"),
            },
        ]
        self.payment_buttons = {
            "btn_forklift_full": {"objectId": "btn_forklift_full", "Name": "קורס מלגזה", "Title": "קורס מלגזה", "Active": True},
            "btn_forklift_deposit": {"objectId": "btn_forklift_deposit", "Name": "מקדמה קורס מלגזה", "Title": "מקדמה", "Active": True},
            "btn_forklift_friday": {"objectId": "btn_forklift_friday", "Name": "קורס מלגזה ימי שישי", "Title": "ימי שישי", "Active": True},
            "btn_refresh": {"objectId": "btn_refresh", "Name": "ריענון מלגזה בודדים", "Title": "ריענון מלגזה", "Active": True},
            "btn_tractor": {"objectId": "btn_tractor", "Name": "קורס טרקטור ישראלים", "Title": "קורס טרקטור", "Active": True},
            "btn_tractor_discount": {"objectId": "btn_tractor_discount", "Name": "10 אחוז הנחה לקורס טרקטור", "Active": True},
            "btn_discount_only": {"objectId": "btn_discount_only", "Name": "15 אחוז הנחה", "Active": True},
        }
        self.rows = [
            row("row_f1", "btn_forklift_full", "prod_forklift", "קורס מלגזה מלא", 1102),
            row("row_f2", "btn_forklift_deposit", "prod_forklift", "מקדמה קורס מלגזה", 254),
            row("row_f3", "btn_forklift_friday", "prod_forklift_friday", "קורס מלגזה ימי שישי", 1186),
            row("row_r1", "btn_refresh", "prod_forklift_refresh", "ריענון מלגזה", 339),
            row("row_t1", "btn_tractor", "prod_tractor", "קורס טרקטור", 2712),
            row("row_t2", "btn_tractor_discount", "prod_tractor", "10 אחוז הנחה", 2000),
            row("row_d1", "btn_discount_only", "prod_discount_only", "הנחה", 100),
        ]

    async def _get_object(self, table_name: str, object_id: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if table_name == "ProductCategories":
            return next((item for item in self.categories if item["objectId"] == object_id), None)
        if table_name == "Products":
            product = next((item for item in self.products if item["objectId"] == object_id), None)
            return self._include_category(product) if product else None
        if table_name == "PaymentBtns":
            return self.payment_buttons.get(object_id)
        return None

    async def _get_class(self, table_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        where = json.loads(params.get("where", "{}"))
        if table_name == "ProductCategories":
            rows = self.categories
            if "Code" in where:
                rows = [item for item in rows if item.get("Code") == where["Code"]]
            return rows
        if table_name == "Products":
            category_id = where.get("Category", {}).get("objectId")
            return [self._include_category(item) for item in self.products if item.get("Category", {}).get("objectId") == category_id]
        if table_name == "PaymentBtnsRows":
            product_ids = {item["objectId"] for item in where.get("ProductId", {}).get("$in", [])}
            return [self._include_row(row_data) for row_data in self.rows if row_data["ProductId"]["objectId"] in product_ids]
        return []

    def _include_category(self, product: dict[str, Any]) -> dict[str, Any]:
        category_id = product.get("Category", {}).get("objectId")
        category = next((item for item in self.categories if item["objectId"] == category_id), None)
        return {**product, "Category": category or product.get("Category")}

    def _include_row(self, row_data: dict[str, Any]) -> dict[str, Any]:
        product = next(item for item in self.products if item["objectId"] == row_data["ProductId"]["objectId"])
        payment_button = self.payment_buttons[row_data["PaymentBtnId"]["objectId"]]
        return {**row_data, "ProductId": self._include_category(product), "PaymentBtnId": payment_button}


def row(row_id: str, button_id: str, product_id: str, description: str, price: int) -> dict[str, Any]:
    return {
        "objectId": row_id,
        "PaymentBtnId": pointer("PaymentBtns", button_id),
        "ProductId": pointer("Products", product_id),
        "ProductDescription": description,
        "Price": price,
    }


def run(coro):
    return asyncio.run(coro)


def test_get_course_payment_links_returns_multiple_valid_links() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="מלגזה"))

    assert result["found"] is True
    assert result["requires_user_choice"] is True
    assert result["required_customer_details"] == REQUIRED_CUSTOMER_DETAILS
    assert {link["payment_btn_id"] for link in result["payment_links"]} == {
        "btn_forklift_full",
        "btn_forklift_deposit",
        "btn_forklift_friday",
    }


def test_get_course_payment_links_can_resolve_by_product_id() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(product_id="prod_forklift_refresh"))

    assert result["found"] is True
    assert result["requires_user_choice"] is False
    assert result["payment_links"][0]["payment_btn_id"] == "btn_refresh"
    assert result["payment_links"][0]["payment_url"].endswith("oid=btn_refresh")


def test_payment_intent_ranks_deposit_first_without_filtering() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="מלגזה", payment_intent="DEPOSIT"))

    assert result["found"] is True
    assert result["payment_links"][0]["payment_btn_id"] == "btn_forklift_deposit"
    assert len(result["payment_links"]) == 3


def test_discount_links_are_restricted_and_not_returned_to_customer() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="טרקטור"))

    assert result["found"] is True
    assert {link["payment_btn_id"] for link in result["payment_links"]} == {"btn_tractor"}
    assert result["restricted_links_summary"] == [
        {
            "payment_btn_id": "btn_tractor_discount",
            "name": "10 אחוז הנחה לקורס טרקטור",
            "restriction_reason": "DISCOUNT_LINK_NOT_ALLOWED",
        }
    ]


def test_only_discount_links_require_representative() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links(category_name="קורס הנחות"))

    assert result["found"] is False
    assert result["requires_representative"] is True
    assert result["payment_links"] == []
    assert result["restricted_links_summary"][0]["payment_btn_id"] == "btn_discount_only"


def test_missing_identifier_returns_missing() -> None:
    service = PaymentLinkService(FakeMyBusiness())

    result = run(service.get_course_payment_links())

    assert result["found"] is False
    assert result["payment_links"] == []
