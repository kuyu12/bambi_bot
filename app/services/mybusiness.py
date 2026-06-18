from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings


ACCOUNT_MATCH_FIELDS = ("PhoneNumber", "StudentPhone", "Phone2", "Phone3", "StudentId", "IdClient", "CompanyId")


class MyBusinessService:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.mybusiness_app_id and self.settings.mybusiness_master_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Parse-Application-Id": self.settings.mybusiness_app_id,
            "X-Parse-Master-Key": self.settings.mybusiness_master_key,
        }

    async def _get_class(self, table_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.is_configured:
            raise RuntimeError("MyBusiness API credentials are not configured.")

        results: list[dict[str, Any]] = []
        limit = int(params.get("limit") or 1000)
        skip = int(params.get("skip") or 0)
        base_params = {**params, "limit": limit}

        async with httpx.AsyncClient(
            base_url=self.settings.mybusiness_base_url.rstrip("/"),
            headers=self._headers(),
            timeout=self.settings.mybusiness_timeout_seconds,
        ) as client:
            while True:
                response = await client.get(f"/classes/{table_name}", params={**base_params, "skip": skip})
                response.raise_for_status()
                batch = response.json().get("results", [])
                results.extend(batch)
                if len(batch) < limit:
                    break
                skip += limit

        return results

    async def find_existing_customer(self, identifier: str) -> dict[str, Any]:
        variants = normalize_identifier_variants(identifier)
        if not variants:
            return {"found": False, "match_count": 0, "returned_count": 0, "identifier_variants": [], "customers": []}
        if not self.is_configured:
            return {
                "found": False,
                "match_count": 0,
                "returned_count": 0,
                "identifier_variants": variants,
                "customers": [],
                "message": "MyBusiness API is not configured.",
            }

        clauses = [{field: value} for value in variants for field in ACCOUNT_MATCH_FIELDS]
        rows = await self._get_class(
            "Accounts",
            {
                "where": json_dumps({"$or": clauses}),
                "limit": 1000,
                "keys": ",".join(
                    [
                        "objectId",
                        "Name",
                        "F_name",
                        "L_name",
                        "Email",
                        "PhoneNumber",
                        "StudentPhone",
                        "Phone2",
                        "Phone3",
                        "StudentId",
                        "IdClient",
                        "CompanyId",
                        "isStudent",
                        "IsAccount",
                        "Delete",
                        "createdAt",
                        "updatedAt",
                    ]
                ),
            },
        )
        customers = [map_customer(row) for row in rows]
        return {
            "found": bool(customers),
            "match_count": len(customers),
            "returned_count": len(customers),
            "identifier_variants": variants,
            "customers": customers,
        }

    async def list_course_categories(self, search: str | None = None) -> dict[str, Any]:
        if not self.is_configured:
            return {"categories_count": 0, "categories": [], "message": "MyBusiness API is not configured."}

        rows = await self._get_class(
            "ProductCategories",
            {
                "limit": 1000,
                "order": "Name",
                "keys": "objectId,Name,Code,createdAt,updatedAt",
            },
        )
        categories = [map_category(row) for row in rows]
        if search:
            needle = normalize_text(search)
            categories = [
                category
                for category in categories
                if needle in normalize_text(category["name"]) or needle in normalize_text(category["code"])
            ]
        return {"categories_count": len(categories), "categories": categories}

    async def find_available_course_dates(
        self,
        category_id: str | None = None,
        category_code: str | None = None,
        category_name: str | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured:
            return {"found": False, "available_courses_count": 0, "courses": [], "message": "MyBusiness API is not configured."}

        category_result = await self._resolve_category(category_id, category_code, category_name)
        if category_result.get("ambiguous") or not category_result.get("category"):
            return {**category_result, "found": False, "courses": []}

        category = category_result["category"]
        open_statuses = await self._get_open_statuses()
        if not open_statuses:
            return {"found": False, "category": category, "available_courses_count": 0, "courses": []}

        now_iso = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        where = {
            "StartDate": {"$gt": {"__type": "Date", "iso": now_iso}},
            "StatusId": {"$in": [pointer("CourseStatuses", status["status_id"]) for status in open_statuses]},
            "ProductCategory": pointer("ProductCategories", category["category_id"]),
        }
        rows = await self._get_class(
            "Courses",
            {
                "where": json_dumps(where),
                "limit": 1000,
                "order": "StartDate",
                "include": "StatusId,ProductCategory,ProductId,FacilityId,MainLecturerId,MainClassId",
                "keys": (
                    "objectId,Name,StartDate,EndDate,FirstClass,StatusId,ProductCategory,ProductId,FacilityId,"
                    "MainLecturerId,MainClassId,MaxCapacity,RegisteredStudents,AllowOverBooking,NumberOfLessons"
                ),
            },
        )

        courses = []
        for row in rows:
            course = map_available_course(row, category)
            if course is not None:
                courses.append(course)

        return {
            "found": bool(courses),
            "category": category,
            "available_courses_count": len(courses),
            "raw_matching_courses_before_capacity_filter": len(rows),
            "courses": courses,
        }

    async def _resolve_category(self, category_id: str | None, category_code: str | None, category_name: str | None) -> dict[str, Any]:
        categories = (await self.list_course_categories()).get("categories", [])
        if category_id:
            matches = [category for category in categories if category["category_id"] == category_id]
        elif category_code:
            normalized_code = normalize_text(category_code)
            matches = [category for category in categories if normalize_text(category["code"]) == normalized_code]
        elif category_name:
            needle = normalize_text(category_name)
            matches = [category for category in categories if needle and needle in normalize_text(category["name"])]
        else:
            return {"found": False, "message": "Missing category_id, category_code, or category_name."}

        if len(matches) == 1:
            return {"found": True, "category": matches[0]}
        if len(matches) > 1:
            return {"found": False, "ambiguous": True, "matches": matches, "courses": []}
        return {"found": False, "message": "No matching course category found.", "matches": []}

    async def _get_open_statuses(self) -> list[dict[str, Any]]:
        rows = await self._get_class(
            "CourseStatuses",
            {
                "where": json_dumps({"IsOpen": True}),
                "limit": 1000,
                "keys": "objectId,Name,IsOpen",
            },
        )
        return [{"status_id": row.get("objectId"), "name": clean(row.get("Name")), "is_open": bool(row.get("IsOpen"))} for row in rows]


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def pointer(class_name: str, object_id: str) -> dict[str, str]:
    return {"__type": "Pointer", "className": class_name, "objectId": object_id}


def clean(value: Any) -> Any:
    return html.unescape(value) if isinstance(value, str) else value


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or "")).strip().lower())


def normalize_identifier_variants(identifier: str) -> list[str]:
    raw = str(identifier or "").strip()
    digits = re.sub(r"\D+", "", raw)
    variants = []
    for value in (raw, digits):
        if value and value not in variants:
            variants.append(value)

    if digits.startswith("972") and len(digits) >= 11:
        local = "0" + digits[3:]
        no_zero = digits[3:]
        for value in (local, no_zero, digits, f"+{digits}"):
            if value not in variants:
                variants.append(value)
    elif digits.startswith("0") and len(digits) >= 9:
        no_zero = digits[1:]
        intl = f"972{no_zero}"
        for value in (no_zero, intl, f"+{intl}"):
            if value not in variants:
                variants.append(value)

    return variants


def map_customer(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": row.get("objectId"),
        "name": clean(row.get("Name")),
        "first_name": clean(row.get("F_name")),
        "last_name": clean(row.get("L_name")),
        "email": clean(row.get("Email")),
        "phone_number": clean(row.get("PhoneNumber")),
        "student_phone": clean(row.get("StudentPhone")),
        "phone2": clean(row.get("Phone2")),
        "phone3": clean(row.get("Phone3")),
        "student_id": clean(row.get("StudentId")),
        "id_client": clean(row.get("IdClient")),
        "company_id": clean(row.get("CompanyId")),
        "is_student": row.get("isStudent"),
        "is_account": row.get("IsAccount"),
        "deleted": row.get("Delete"),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
    }


def map_category(row: dict[str, Any]) -> dict[str, Any]:
    name = clean(row.get("Name")) or ""
    return {
        "category_id": row.get("objectId"),
        "name": name,
        "code": clean(row.get("Code")),
        "normalized_name": normalize_text(name),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
    }


def map_available_course(row: dict[str, Any], category: dict[str, Any]) -> dict[str, Any] | None:
    max_capacity = row.get("MaxCapacity")
    if max_capacity is None:
        return None
    registered_students = row.get("RegisteredStudents") or 0
    available_seats = int(max_capacity) - int(registered_students)
    if available_seats <= 0:
        return None

    status = row.get("StatusId") or {}
    product = row.get("ProductId") or {}
    facility = row.get("FacilityId") or {}
    lecturer = row.get("MainLecturerId") or {}
    class_obj = row.get("MainClassId") or {}
    return {
        "course_id": row.get("objectId"),
        "course_name": clean(row.get("Name")),
        "start_date": parse_date(row.get("StartDate")),
        "end_date": parse_date(row.get("EndDate")),
        "first_class": parse_date(row.get("FirstClass")),
        "available_seats": available_seats,
        "max_capacity": max_capacity,
        "registered_students": registered_students,
        "allow_overbooking": row.get("AllowOverBooking"),
        "status": clean(status.get("Name")) if isinstance(status, dict) else None,
        "status_id": status.get("objectId") if isinstance(status, dict) else None,
        "facility": clean(facility.get("Name")) if isinstance(facility, dict) else None,
        "facility_id": facility.get("objectId") if isinstance(facility, dict) else None,
        "lecturer": clean(lecturer.get("Name")) if isinstance(lecturer, dict) else None,
        "lecturer_id": lecturer.get("objectId") if isinstance(lecturer, dict) else None,
        "class_name": clean(class_obj.get("Name")) if isinstance(class_obj, dict) else None,
        "class_id": class_obj.get("objectId") if isinstance(class_obj, dict) else None,
        "product_id": product.get("objectId") if isinstance(product, dict) else None,
        "product_name": clean(product.get("Name")) if isinstance(product, dict) else None,
        "product_price": product.get("Price") if isinstance(product, dict) else None,
        "product_catalog_number": clean(product.get("CatalogNumber")) if isinstance(product, dict) else None,
        "category_id": category["category_id"],
        "category_name": category["name"],
        "category_code": category["code"],
        "number_of_lessons": row.get("NumberOfLessons"),
    }


def parse_date(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("iso")
    if isinstance(value, str):
        return value
    return None
