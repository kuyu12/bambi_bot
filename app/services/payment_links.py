from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.mybusiness import normalize_course_search, normalize_text


PAYMENT_LINKS_PATH = Path(__file__).resolve().parents[1] / "tools-knowleage" / "payment_links.json"


@dataclass(frozen=True)
class PaymentCourseRecord:
    course_key: str
    course_name: str
    aliases: list[str]
    required_customer_details: list[str]
    payment_link: str
    payment_note: str | None = None
    amount: float | None = None
    currency: str = "ILS"
    extra: dict[str, Any] | None = None


class PaymentLinkService:
    def __init__(self, path: Path = PAYMENT_LINKS_PATH):
        self.path = path
        self.records = self._load_records()

    def find_payment_instructions(self, course_name: str) -> dict[str, Any]:
        query = normalize_course_search(course_name)
        if not query:
            return {
                "found": False,
                "matches_count": 0,
                "matches": [],
                "message": "Missing course name.",
            }

        exact_matches = [record for record in self.records if query in normalized_record_terms(record)]
        if len(exact_matches) == 1:
            return format_record(exact_matches[0])
        if len(exact_matches) > 1:
            return format_ambiguous(exact_matches)

        fuzzy_matches = [
            record
            for record in self.records
            if any(query in term or term in query for term in normalized_record_terms(record))
        ]
        if len(fuzzy_matches) == 1:
            return format_record(fuzzy_matches[0])
        if len(fuzzy_matches) > 1:
            return format_ambiguous(fuzzy_matches)

        return {
            "found": False,
            "matches_count": 0,
            "matches": [],
            "message": "No payment link is configured for this course.",
        }

    def allowed_payment_urls(self) -> set[str]:
        urls = set()
        for record in self.records:
            urls.add(record.payment_link)
            if record.extra:
                health_link = record.extra.get("health_declaration_link")
                if isinstance(health_link, str) and health_link:
                    urls.add(health_link)
        return urls

    def _load_records(self) -> list[PaymentCourseRecord]:
        if not self.path.exists():
            return []

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        records = []
        for item in payload.get("courses", []):
            payment_link = str(item.get("payment_link") or "").strip()
            if not payment_link:
                continue
            records.append(
                PaymentCourseRecord(
                    course_key=str(item.get("course_key") or slugify(item.get("course_name"))),
                    course_name=str(item["course_name"]),
                    aliases=[str(alias) for alias in item.get("aliases", [])],
                    required_customer_details=[str(detail) for detail in item.get("required_customer_details", [])],
                    payment_link=payment_link,
                    payment_note=str(item["payment_note"]) if item.get("payment_note") else None,
                    amount=item.get("amount"),
                    currency=str(item.get("currency") or "ILS"),
                    extra={
                        key: value
                        for key, value in item.items()
                        if key
                        not in {
                            "course_key",
                            "course_name",
                            "aliases",
                            "required_customer_details",
                            "payment_link",
                            "payment_note",
                            "amount",
                            "currency",
                        }
                    },
                )
            )
        return records


def normalized_record_terms(record: PaymentCourseRecord) -> set[str]:
    values = [record.course_key, record.course_name, *record.aliases]
    terms = {normalize_text(value) for value in values}
    terms.update(normalize_course_search(value) for value in values)
    return {term for term in terms if term}


def format_record(record: PaymentCourseRecord) -> dict[str, Any]:
    return {
        "found": True,
        "matches_count": 1,
        "course": {
            "course_key": record.course_key,
            "course_name": record.course_name,
            "required_customer_details": record.required_customer_details,
            "payment_link": record.payment_link,
            "payment_note": record.payment_note,
            "amount": record.amount,
            "currency": record.currency,
            **(record.extra or {}),
        },
    }


def format_ambiguous(records: list[PaymentCourseRecord]) -> dict[str, Any]:
    return {
        "found": False,
        "ambiguous": True,
        "matches_count": len(records),
        "matches": [
            {
                "course_key": record.course_key,
                "course_name": record.course_name,
                "aliases": record.aliases,
            }
            for record in records
        ],
        "message": "More than one payment link matched. Ask the customer which course they mean.",
    }


def slugify(value: Any) -> str:
    text = normalize_course_search(value)
    text = re.sub(r"[^a-zA-Z0-9\u0590-\u05ff]+", "_", text)
    return text.strip("_") or "course"
