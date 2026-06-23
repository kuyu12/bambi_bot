from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContactChannel:
    owner: str
    phone: str
    family: str
    keywords: tuple[str, ...]


CONTACT_CHANNELS = [
    ContactChannel(
        owner="טלי",
        phone="052-702-3884",
        family="ימי עיון קצב\"ט וממונה, אבטחת מטענים וקורסי מדריכים",
        keywords=(
            "קצין בטיחות",
            "קצבט",
            "ממונה בטיחות",
            "ימי עיון",
            "יום עיון",
            "אבטחת מטענים",
            "מדריך",
            "מדריכים",
            "הדרכה טובה",
            "חקירת תאונות",
        ),
    ),
    ContactChannel(
        owner="חן",
        phone="054-904-7872",
        family="עבודה בגובה ועגורנים",
        keywords=(
            "עבודה בגובה",
            "גובה",
            "עגורן",
            "מנוף",
            "אתת",
            "אתתים",
            "עגורן גשר",
            "העמסה עצמית",
            "חידוש רישיון מנוף",
            "חידוש תעודת עגורן",
        ),
    ),
    ContactChannel(
        owner="ירין",
        phone="054-904-7652",
        family="טרקטור, מכונה ניידת ומשא כבד",
        keywords=(
            "טרקטור",
            "מכונה ניידת",
            "צמה",
            "צמ\"ה",
            "משא כבד",
            "משאית",
            "c1",
            "c",
        ),
    ),
    ContactChannel(
        owner="מרינה",
        phone="054-580-6131",
        family="הובלת חומ\"ס, רישיון מוביל, מדריכי מלגזות ורכב ציבורי",
        keywords=(
            "חומס",
            "חומ\"ס",
            "חומ״ס",
            "חומרים מסוכנים",
            "הובלת חומס",
            "אחראי שינוע",
            "רישיון מוביל",
            "רשיון מוביל",
            "מוביל",
            "מדריך מלגזה",
            "מדריכי מלגזה",
            "רכב ציבורי",
            "אוטובוס",
            "d1",
            "d",
            "מונית",
        ),
    ),
    ContactChannel(
        owner="מרינה",
        phone="054-968-8028",
        family="מלגזות ורענוני מלגזה",
        keywords=(
            "מלגזה",
            "מלגזות",
            "רענון מלגזה",
            "ריענון מלגזה",
            "רענון שנתי מלגזה",
        ),
    ),
]

OFFICE_CONTACT = {
    "owner": "משרד",
    "phone": "074-70-87-030",
    "family": "פרטי משרד כלליים",
}


class ContactChannelService:
    def find_course_contact(self, course_name: str | None) -> dict[str, Any]:
        query = normalize_contact_query(course_name)
        if not query:
            return {
                "found": False,
                "ambiguous": False,
                "matches_count": 0,
                "matches": [],
                "fallback": OFFICE_CONTACT,
                "message": "Missing course name. Use office contact only for general inquiries.",
            }

        matches = [
            channel
            for channel in CONTACT_CHANNELS
            if any(normalize_contact_query(keyword) in query for keyword in channel.keywords)
        ]

        # Instructor forklift belongs to the instructor/contact family, not the general forklift family.
        if any(term in query for term in ("מדריך מלגזה", "מדריכי מלגזה")):
            matches = [channel for channel in matches if channel.phone == "054-580-6131"]

        unique = dedupe_channels(matches)
        if len(unique) == 1:
            channel = unique[0]
            return {
                "found": True,
                "ambiguous": False,
                "matches_count": 1,
                "contact": format_channel(channel),
                "fallback": OFFICE_CONTACT,
            }
        if len(unique) > 1:
            return {
                "found": False,
                "ambiguous": True,
                "matches_count": len(unique),
                "matches": [format_channel(channel) for channel in unique],
                "fallback": OFFICE_CONTACT,
                "message": "More than one course contact family matched. Ask a short clarification question.",
            }

        return {
            "found": False,
            "ambiguous": False,
            "matches_count": 0,
            "matches": [],
            "fallback": OFFICE_CONTACT,
            "message": "No course-specific contact was matched. Use office contact only if no course family is clear.",
        }


def normalize_contact_query(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("ריענון", "רענון")
    text = text.replace('חומ"ס', "חומס").replace("חומ״ס", "חומס")
    text = re.sub(r"[\"'״׳`´]+", "", text)
    text = re.sub(r"[^\w\u0590-\u05ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_channels(channels: list[ContactChannel]) -> list[ContactChannel]:
    seen = set()
    unique = []
    for channel in channels:
        if channel.phone in seen:
            continue
        seen.add(channel.phone)
        unique.append(channel)
    return unique


def format_channel(channel: ContactChannel) -> dict[str, str]:
    return {
        "owner": channel.owner,
        "phone": channel.phone,
        "family": channel.family,
        "channel": "whatsapp",
    }
