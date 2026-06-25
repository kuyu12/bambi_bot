from __future__ import annotations

import re
from typing import Any

from app.services.mybusiness import clean, json_dumps, normalize_text, pointer


TENANT_DOMAIN = "6a09b3ab-e66c-64a7-7dbc-06c797b56505.mbapps.co.il"
PAYMENT_URL_BASE = f"https://{TENANT_DOMAIN}/apps/mybooks/payment-btn-page?cls=PaymentBtns&oid="
REQUIRED_CUSTOMER_DETAILS = ["שם מלא", "מספר טלפון", "תעודת זהות", "מייל"]
PAYMENT_INTENTS = {"FULL", "DEPOSIT", "REFRESHER", "FRIDAY", "THEORY", "PRACTICAL", "EXAM", "GENERAL"}
DISCOUNT_KEYWORDS = ("הנחה", "אחוז הנחה", "10 אחוז", "15 אחוז", "discount")


class PaymentLinkService:
    def __init__(self, mybusiness: Any):
        self.mybusiness = mybusiness

    async def get_course_payment_links(
        self,
        category_id: str | None = None,
        category_code: str | None = None,
        category_name: str | None = None,
        product_id: str | None = None,
        payment_intent: str | None = None,
        include_restricted: bool = False,
    ) -> dict[str, Any]:
        if not any([category_id, category_code, category_name, product_id]):
            return {
                "found": False,
                "requires_user_choice": False,
                "requires_representative": False,
                "payment_links": [],
                "restricted_links_summary": [],
                "reason": "Missing category_id, category_code, category_name, or product_id.",
            }
        if not self.mybusiness.is_configured:
            return {
                "found": False,
                "requires_user_choice": False,
                "requires_representative": True,
                "payment_links": [],
                "restricted_links_summary": [],
                "reason": "MyBusiness API is not configured.",
            }

        intent = normalize_payment_intent(payment_intent)
        product_result = await self._resolve_products(category_id, category_code, category_name, product_id)
        if not product_result.get("found"):
            return product_result

        category = product_result.get("category")
        products = product_result.get("products") or []
        if not products:
            return {
                "found": False,
                "requires_user_choice": False,
                "requires_representative": False,
                "category": category,
                "payment_links": [],
                "restricted_links_summary": [],
                "reason": "No products were found for the requested course category.",
            }

        rows = await self._get_payment_rows_for_products([product["objectId"] for product in products if product.get("objectId")])
        product_by_id = {product["objectId"]: product for product in products if product.get("objectId")}
        payment_links: list[dict[str, Any]] = []
        restricted_links: list[dict[str, Any]] = []
        seen_links: set[tuple[str | None, str | None, str | None]] = set()

        for row in rows:
            product = row.get("ProductId") if isinstance(row.get("ProductId"), dict) else None
            product_id_from_row = product.get("objectId") if product else None
            product = product_by_id.get(product_id_from_row) or product or {}
            payment_btn = await self._resolve_payment_button(row)
            if not payment_btn:
                continue
            if payment_btn.get("Active") is False:
                continue

            if is_discount_link(payment_btn, row, product):
                restricted_links.append(format_restricted_link(payment_btn))
                continue

            link = format_payment_link(payment_btn, row, product)
            dedupe_key = (link.get("payment_btn_id"), link.get("product", {}).get("product_id"), str(link.get("row_price")))
            if dedupe_key in seen_links:
                continue
            seen_links.add(dedupe_key)
            payment_links.append(link)

        payment_links = rank_links_by_payment_intent(payment_links, intent)
        restricted_links = dedupe_restricted_links(restricted_links)

        if not payment_links and restricted_links:
            return {
                "found": False,
                "requires_user_choice": False,
                "requires_representative": True,
                "category": category,
                "payment_links": [],
                "restricted_links_summary": restricted_links,
                "reason": "Only discount payment links were found. The bot is not authorized to provide discount links.",
                "required_customer_details": REQUIRED_CUSTOMER_DETAILS,
            }

        return {
            "found": bool(payment_links),
            "requires_user_choice": len(payment_links) > 1,
            "requires_representative": False,
            "category": category,
            "products_count": len(products),
            "payment_links": payment_links,
            "restricted_links_summary": restricted_links,
            "required_customer_details": REQUIRED_CUSTOMER_DETAILS,
            "reason": None if payment_links else "No payment links were found for the requested course category or product.",
            "include_restricted": include_restricted,
        }

    def allowed_payment_urls(self) -> set[str]:
        # Dynamic payment links are validated by domain/path in the output guardrail.
        return set()

    async def _resolve_products(
        self,
        category_id: str | None,
        category_code: str | None,
        category_name: str | None,
        product_id: str | None,
    ) -> dict[str, Any]:
        if product_id:
            product = await self.mybusiness._get_object("Products", product_id, {"include": "Category"})
            if not product:
                return not_found("Product was not found.")
            category = map_category_ref(product.get("Category"))
            return {"found": True, "category": category, "products": [product]}

        category_result = await self._resolve_category(category_id, category_code, category_name)
        if not category_result.get("found"):
            return category_result

        category = category_result["category"]
        products = await self._get_products_for_category(category["category_id"])
        active_or_unspecified = [product for product in products if product.get("IsActive") is not False]
        return {"found": True, "category": category, "products": active_or_unspecified or products}

    async def _resolve_category(
        self,
        category_id: str | None,
        category_code: str | None,
        category_name: str | None,
    ) -> dict[str, Any]:
        if category_id:
            row = await self.mybusiness._get_object("ProductCategories", category_id)
            if not row:
                return not_found("Course category was not found.")
            return {"found": True, "category": map_category_ref(row)}

        if category_code:
            rows = await self.mybusiness._get_class(
                "ProductCategories",
                {
                    "where": json_dumps({"Code": str(category_code)}),
                    "limit": 100,
                    "keys": "objectId,Name,Code,createdAt,updatedAt",
                },
            )
            return resolve_category_matches(rows)

        rows = await self.mybusiness._get_class(
            "ProductCategories",
            {
                "limit": 1000,
                "order": "Name",
                "keys": "objectId,Name,Code,createdAt,updatedAt",
            },
        )
        query = normalize_payment_text(category_name)
        exact = [row for row in rows if normalize_payment_text(row.get("Name")) == query]
        if exact:
            return resolve_category_matches(exact)

        partial = [row for row in rows if query and query in normalize_payment_text(row.get("Name"))]
        return resolve_category_matches(partial)

    async def _get_products_for_category(self, category_id: str) -> list[dict[str, Any]]:
        return await self.mybusiness._get_class(
            "Products",
            {
                "where": json_dumps({"Category": pointer("ProductCategories", category_id)}),
                "limit": 1000,
                "include": "Category",
                "keys": "objectId,Name,CatalogNumber,Price,IsActive,Category,createdAt,updatedAt",
            },
        )

    async def _get_payment_rows_for_products(self, product_ids: list[str]) -> list[dict[str, Any]]:
        if not product_ids:
            return []
        return await self.mybusiness._get_class(
            "PaymentBtnsRows",
            {
                "where": json_dumps({"ProductId": {"$in": [pointer("Products", product_id) for product_id in product_ids]}}),
                "limit": 1000,
                "include": "PaymentBtnId,ProductId,ProductId.Category",
                "keys": "objectId,PaymentBtnId,ProductId,ProductDescription,Price,MinQuantity,MaxQuantity,CurrencyRate,createdAt,updatedAt",
            },
        )

    async def _resolve_payment_button(self, row: dict[str, Any]) -> dict[str, Any] | None:
        payment_btn = row.get("PaymentBtnId")
        if isinstance(payment_btn, dict) and payment_btn.get("objectId") and (payment_btn.get("Name") or payment_btn.get("Title")):
            return payment_btn
        payment_btn_id = payment_btn.get("objectId") if isinstance(payment_btn, dict) else None
        if not payment_btn_id:
            return None
        return await self.mybusiness._get_object(
            "PaymentBtns",
            payment_btn_id,
            {
                "keys": (
                    "objectId,Name,Title,TopParagraph,Footer,Link,Active,OrdersCount,IsPayments,"
                    "AllowedPayments,NoVat,VatPercent,RoundTotal,createdAt,updatedAt"
                )
            },
        )


def resolve_category_matches(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) == 1:
        return {"found": True, "category": map_category_ref(rows[0])}
    if len(rows) > 1:
        return {
            "found": False,
            "ambiguous_category": True,
            "matches_count": len(rows),
            "matches": [map_category_ref(row) for row in rows],
            "payment_links": [],
            "restricted_links_summary": [],
        }
    return not_found("No matching course category found.")


def not_found(reason: str) -> dict[str, Any]:
    return {
        "found": False,
        "requires_user_choice": False,
        "requires_representative": False,
        "payment_links": [],
        "restricted_links_summary": [],
        "reason": reason,
        "required_customer_details": REQUIRED_CUSTOMER_DETAILS,
    }


def map_category_ref(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {
        "category_id": row.get("objectId"),
        "category_name": clean(row.get("Name")),
        "category_code": clean(row.get("Code")),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
    }


def format_payment_link(payment_btn: dict[str, Any], row: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    payment_btn_id = payment_btn.get("objectId")
    name = clean(payment_btn.get("Name")) or clean(payment_btn.get("Title"))
    title = clean(payment_btn.get("Title"))
    row_price = row.get("Price")
    product_name = clean(product.get("Name"))
    return {
        "payment_btn_id": payment_btn_id,
        "name": name,
        "title": title,
        "active": payment_btn.get("Active"),
        "row_price": row_price,
        "product": {
            "product_id": product.get("objectId"),
            "product_name": product_name,
            "catalog_number": clean(product.get("CatalogNumber")),
            "product_price": product.get("Price"),
            "product_is_active": product.get("IsActive"),
        },
        "is_payments": payment_btn.get("IsPayments"),
        "allowed_payments": payment_btn.get("AllowedPayments"),
        "orders_count": payment_btn.get("OrdersCount"),
        "payment_url": build_payment_url(payment_btn_id, payment_btn.get("Link")),
        "description_for_bot": build_description(name, title, row.get("ProductDescription"), product_name, row_price),
    }


def build_payment_url(payment_btn_id: str | None, link_field: Any) -> str | None:
    link = str(link_field or "").strip()
    if link:
        return link
    if not payment_btn_id:
        return None
    return f"{PAYMENT_URL_BASE}{payment_btn_id}"


def is_approved_dynamic_payment_url(url: str) -> bool:
    cleaned = str(url or "").rstrip(".,;:!?")
    return (
        cleaned.startswith(f"https://{TENANT_DOMAIN}/apps/mybooks/payment-btn-page")
        and "cls=PaymentBtns" in cleaned
        and "oid=" in cleaned
    )


def build_description(name: Any, title: Any, row_description: Any, product_name: Any, price: Any) -> str:
    parts = [clean(item) for item in (name, title, row_description, product_name) if clean(item)]
    text = " - ".join(dict.fromkeys(str(item) for item in parts))
    if price is not None:
        text = f"{text} - מחיר {price}" if text else f"מחיר {price}"
    return text


def is_discount_link(payment_btn: dict[str, Any], row: dict[str, Any], product: dict[str, Any]) -> bool:
    fields = [
        payment_btn.get("Name"),
        payment_btn.get("Title"),
        payment_btn.get("TopParagraph"),
        payment_btn.get("Footer"),
        row.get("ProductDescription"),
        product.get("Name"),
    ]
    text = normalize_payment_text(" ".join(str(clean(field) or "") for field in fields))
    return any(keyword in text for keyword in DISCOUNT_KEYWORDS)


def format_restricted_link(payment_btn: dict[str, Any]) -> dict[str, str | None]:
    return {
        "payment_btn_id": payment_btn.get("objectId"),
        "name": clean(payment_btn.get("Name")) or clean(payment_btn.get("Title")),
        "restriction_reason": "DISCOUNT_LINK_NOT_ALLOWED",
    }


def dedupe_restricted_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for link in links:
        key = link.get("payment_btn_id") or link.get("name")
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def normalize_payment_intent(value: str | None) -> str:
    intent = str(value or "GENERAL").strip().upper()
    return intent if intent in PAYMENT_INTENTS else "GENERAL"


def rank_links_by_payment_intent(links: list[dict[str, Any]], intent: str) -> list[dict[str, Any]]:
    if intent == "GENERAL":
        return links
    return sorted(links, key=lambda link: intent_score(link, intent), reverse=True)


def intent_score(link: dict[str, Any], intent: str) -> int:
    text = normalize_payment_text(
        " ".join(
            str(item or "")
            for item in [
                link.get("name"),
                link.get("title"),
                link.get("description_for_bot"),
                link.get("product", {}).get("product_name"),
            ]
        )
    )
    positive = {
        "DEPOSIT": ("מקדמה", "דמי רישום"),
        "REFRESHER": ("רענון", "ריענון"),
        "FRIDAY": ("שישי", "יום ו"),
        "THEORY": ("תאוריה", "תיאוריה", "עיוני"),
        "PRACTICAL": ("מעשי",),
        "EXAM": ("בחינה", "בחינות", "חידוש"),
    }
    negative_for_full = ("מקדמה", "רענון", "ריענון", "הנחה", "תאוריה", "תיאוריה", "מעשי")
    if intent == "FULL":
        return 10 - sum(1 for token in negative_for_full if token in text)
    return sum(5 for token in positive.get(intent, ()) if token in text)


def normalize_payment_text(value: Any) -> str:
    text = normalize_text(value)
    text = text.replace("ריענון", "רענון")
    text = text.replace('חומ"ס', "חומס").replace("חומ״ס", "חומס").replace("חו מס", "חומס")
    text = re.sub(r"[\"'״׳`´]+", "", text)
    text = re.sub(r"[^\w\u0590-\u05ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
