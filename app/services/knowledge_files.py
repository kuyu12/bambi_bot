from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KnowledgeToolSpec:
    tool_id: str
    display_name: str
    description: str
    file_name: str | None


KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "tools-knowleage"
GENERATED_TOOLS_MANIFEST = KNOWLEDGE_DIR / "generated_tools_manifest.json"

KNOWLEDGE_TOOLS: dict[str, KnowledgeToolSpec] = {
    "driving_and_transportation_courses": KnowledgeToolSpec(
        "driving_and_transportation_courses",
        "קורסי נהיגה ותחבורה",
        "מידע מפורט על קורסי נהיגה ותחבורה במכללת במבי.",
        "driving_and_transportation_courses.txt",
    ),
    "safety_courses": KnowledgeToolSpec(
        "safety_courses",
        "קורסי בטיחות",
        "מידע מקיף על קורסי הבטיחות שמציעה מכללת במבי.",
        "safety_courses.txt",
    ),
    "general_safety_at_work": KnowledgeToolSpec(
        "general_safety_at_work",
        "שאלות נפוצות - בטיחות כללית",
        "שאלות ותשובות על הדרכות בטיחות כלליות באתר במבי.",
        "general_safety_at_work.txt",
    ),
    "safety_trustee_course": KnowledgeToolSpec(
        "safety_trustee_course",
        "שאלות נפוצות - קורס נאמני בטיחות",
        "שאלות ותשובות על קורס נאמני בטיחות.",
        "safety_trustee_course.txt",
    ),
    "mobile_machine_license": KnowledgeToolSpec(
        "mobile_machine_license",
        "שאלות נפוצות - רישיון מכונה ניידת",
        "שאלות ותשובות על קורס רישיון מכונה ניידת.",
        "mobile_machine_license.txt",
    ),
    "personal_protective_equipment": KnowledgeToolSpec(
        "personal_protective_equipment",
        "שאלות נפוצות - ציוד מגן אישי",
        "שאלות ותשובות על רכישת ציוד מגן אישי.",
        "personal_protective_equipment.txt",
    ),
    "forklift_instructor_course": KnowledgeToolSpec(
        "forklift_instructor_course",
        "שאלות נפוצות - קורס מדריך מלגזה",
        "שאלות ותשובות על קורס מדריכי מלגזה.",
        "forklift_instructor_course.txt",
    ),
    "work_at_height_instructor_course": KnowledgeToolSpec(
        "work_at_height_instructor_course",
        "שאלות נפוצות - קורס מדריכי עבודה בגובה",
        "שאלות ותשובות על קורס מדריכי עבודה בגובה.",
        "work_at_height_instructor_course.txt",
    ),
    "tractor_course": KnowledgeToolSpec(
        "tractor_course",
        "שאלות נפוצות - קורס טרקטור",
        "שאלות ותשובות על קורס טרקטור.",
        "tractor_course.txt",
    ),
    "self_loading_crane_course": KnowledgeToolSpec(
        "self_loading_crane_course",
        "שאלות נפוצות - קורס עגורן העמסה עצמית",
        "שאלות נפוצות על קורס עגורן העמסה עצמית.",
        "self_loading_crane_course.txt",
    ),
    "regulation_168": KnowledgeToolSpec(
        "regulation_168",
        "תקנה 168 - שעות נהיגה ומנוחה",
        "מידע על תקנה 168, שעות נהיגה ומנוחה לנהגי רכב ציבורי וכבד.",
        "regulation_168.txt",
    ),
    "laws_and_regulations": KnowledgeToolSpec(
        "laws_and_regulations",
        "חוקים ותקנות",
        "חוקים ותקנות בתחום הבטיחות והפיקוח.",
        "laws_and_regulations.txt",
    ),
    "about_the_college": KnowledgeToolSpec(
        "about_the_college",
        "אודות המכללה",
        "רקע, היסטוריה ופעילות של מכללת במבי.",
        "about_the_college.txt",
    ),
    "directions_to_bambi_college": KnowledgeToolSpec(
        "directions_to_bambi_college",
        "דרכי הגעה למכללת במבי",
        "מידע מפורט להגעה למכללת במבי ואזורי הכשרה.",
        "directions_to_bambi_college.txt",
    ),
    "contacting_bambi_college": KnowledgeToolSpec(
        "contacting_bambi_college",
        "יצירת קשר עם מכללת במבי",
        "פרטי התקשרות עם מכללת במבי.",
        "contacting_bambi_college.txt",
    ),
    "customer_info": KnowledgeToolSpec(
        "customer_info",
        "מידע על הלקוח",
        "מידע על לקוח לפי מספר טלפון או תעודת זהות. בעתיד יחובר ל-myBusiness.",
        None,
    ),
}


class KnowledgeFileService:
    def __init__(self) -> None:
        self._tools = self._load_tools()

    def _load_tools(self) -> dict[str, KnowledgeToolSpec]:
        if not GENERATED_TOOLS_MANIFEST.exists():
            return KNOWLEDGE_TOOLS

        payload = json.loads(GENERATED_TOOLS_MANIFEST.read_text(encoding="utf-8"))
        tools: dict[str, KnowledgeToolSpec] = {}
        for item in payload.get("tools", []):
            tool_id = str(item["tool_id"])
            tools[tool_id] = KnowledgeToolSpec(
                tool_id=tool_id,
                display_name=str(item["display_name"]),
                description=str(item["description"]),
                file_name=str(item["file_name"]),
            )

        tools["customer_info"] = KNOWLEDGE_TOOLS["customer_info"]
        return tools or KNOWLEDGE_TOOLS

    def tool_specs(self) -> list[KnowledgeToolSpec]:
        return list(self._tools.values())

    def read_tool_file(self, tool_id: str) -> dict[str, object]:
        spec = self._tools[tool_id]
        if spec.file_name is None:
            return {
                "tool_id": spec.tool_id,
                "tool_name": spec.display_name,
                "description": spec.description,
                "found": False,
                "content": "",
                "message": "לא נמצא מידע על הלקוח במערכת המקומית. החיבור ל-myBusiness עדיין לא פעיל.",
            }

        path = KNOWLEDGE_DIR / spec.file_name
        if not path.exists():
            return {
                "tool_id": spec.tool_id,
                "tool_name": spec.display_name,
                "description": spec.description,
                "file_name": spec.file_name,
                "found": False,
                "content": "",
                "message": "קובץ הידע לא נמצא.",
            }

        return {
            "tool_id": spec.tool_id,
            "tool_name": spec.display_name,
            "description": spec.description,
            "file_name": spec.file_name,
            "found": True,
            "content": path.read_text(encoding="utf-8"),
        }

    def list_tools(self) -> list[dict[str, str | None]]:
        return [
            {
                "tool_id": spec.tool_id,
                "tool_name": spec.display_name,
                "description": spec.description,
                "file_name": spec.file_name,
            }
            for spec in self._tools.values()
        ]
