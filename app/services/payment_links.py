from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.mybusiness import normalize_course_search


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
        query = normalize_payment_course_name(course_name)
        if not query:
            return {
                "found": False,
                "matches_count": 0,
                "matches": [],
                "message": "Missing course name.",
            }

        broad_matches = records_containing_all_query_tokens(query, self.records)
        if is_broad_ambiguous_query(course_name, query) and len(broad_matches) > 1:
            return format_ambiguous(broad_matches)

        exact_matches = [record for record in self.records if query in normalized_record_terms(record)]
        if len(exact_matches) == 1:
            return format_record(exact_matches[0])
        if len(exact_matches) > 1:
            return format_ambiguous(exact_matches)

        fuzzy_matches = high_confidence_fuzzy_matches(course_name, query, self.records)
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
    terms = {normalize_payment_text(value) for value in values}
    terms.update(normalize_payment_course_name(value) for value in values)
    return {term for term in terms if term}


def normalize_payment_text(value: Any) -> str:
    text = normalize_course_search(value)
    text = text.replace("ריענון", "רענון")
    text = text.replace('חומ"ס', "חומס").replace("חומ״ס", "חומס").replace("חו מס", "חומס")
    text = re.sub(r"[\"'״׳`´]+", "", text)
    text = re.sub(r"[^\w\u0590-\u05ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_payment_course_name(value: Any) -> str:
    text = normalize_payment_text(value)
    text = re.sub(r"\bקורס(?:י|ים)?\b", " ", text)
    text = re.sub(r"\bלימוד(?:י|ים)?\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def high_confidence_fuzzy_matches(query_raw: str, query: str, records: list[PaymentCourseRecord]) -> list[PaymentCourseRecord]:
    query_tokens = set(query.split())
    if not query_tokens:
        return []

    candidates = []
    for record in records:
        if course_type_conflicts(query_raw, record):
            continue
        best_score = 0.0
        for term in normalized_record_terms(record):
            term_tokens = set(term.split())
            if not term_tokens:
                continue
            overlap = query_tokens & term_tokens
            if not overlap:
                continue
            precision = len(overlap) / len(query_tokens)
            recall = len(overlap) / len(term_tokens)
            score = min(precision, recall)
            best_score = max(best_score, score)
        if best_score >= 0.75:
            candidates.append((best_score, record))

    if not candidates:
        return []

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score = candidates[0][0]
    return [record for score, record in candidates if score == best_score]


def records_containing_all_query_tokens(query: str, records: list[PaymentCourseRecord]) -> list[PaymentCourseRecord]:
    query_tokens = set(query.split())
    if not query_tokens:
        return []

    matches = []
    for record in records:
        record_tokens = set()
        for term in normalized_record_terms(record):
            record_tokens.update(term.split())
        if query_tokens <= record_tokens:
            matches.append(record)
    return matches


def is_broad_ambiguous_query(query_raw: str, query: str) -> bool:
    raw = normalize_payment_text_without_course_cleanup(query_raw)
    clear_disambiguators = {"קורס", "רענון", "חידוש", "מדריך", "מדריכי", "אחראי", "שינוע", "רכב", "ציבורי"}
    if set(raw.split()) & clear_disambiguators:
        return False
    tokens = set(query.split())
    if len(tokens) <= 1:
        return True
    broad_terms = {"חומס", "מלגזה", "מנוף", "עגורן", "מוביל", "מכונה", "ניידת"}
    return tokens <= broad_terms


def course_type_conflicts(query_raw: str, record: PaymentCourseRecord) -> bool:
    query = normalize_payment_text_without_course_cleanup(query_raw)
    record_text = normalize_payment_text(" ".join([record.course_name, *record.aliases]))
    query_is_refresh = "רענון" in query or "חידוש" in query
    record_is_refresh = "רענון" in record_text or "חידוש" in record_text
    query_is_course = "קורס" in query or "לימודי" in query
    return query_is_course and record_is_refresh and not query_is_refresh


def normalize_payment_text_without_course_cleanup(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("ריענון", "רענון")
    text = text.replace('חומ"ס', "חומס").replace("חומ״ס", "חומס").replace("חו מס", "חומס")
    text = re.sub(r"[\"'״׳`´]+", "", text)
    text = re.sub(r"[^\w\u0590-\u05ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
