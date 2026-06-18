from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field


DEFAULT_RAW_DIR = Path("data/website_raw")
DEFAULT_TOOLS_DIR = Path("app/tools-knowleage")
GENERATED_DIR_NAME = "generated"
GENERATED_MANIFEST_NAME = "generated_tools_manifest.json"
MAX_CATEGORY_CONTENT_CHARS = 35_000
MAX_PAGE_TEXT_CHARS = 28_000

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class KnowledgeBuildDecision(BaseModel):
    has_bot_value: bool = Field(description="Whether this page contains useful Bambi bot knowledge.")
    category_action: Literal["existing", "new", "skip"]
    category_tool_id: str | None = Field(description="Existing or new machine-safe tool id, snake_case English.")
    display_name: str | None = Field(description="Hebrew user-facing category/tool display name.")
    tool_description: str | None = Field(description="Hebrew tool description for the agent.")
    summary_document: str = Field(description="Updated Hebrew knowledge document for this category.")
    reason: str = Field(description="Short explanation of the decision.")
    confidence: Literal["low", "medium", "high"]


@dataclass
class CategoryState:
    tool_id: str
    display_name: str
    description: str
    file_name: str
    content: str = ""
    source_count: int = 0
    source_files: list[str] = field(default_factory=list)


SEED_CATEGORIES = [
    {
        "tool_id": "driving_and_transportation_courses",
        "display_name": "קורסי נהיגה ותחבורה",
        "description": "מידע על קורסי נהיגה ותחבורה במכללת במבי, כולל תנאי קבלה, מבנה, משך, מחירים ומועדים.",
    },
    {
        "tool_id": "safety_courses",
        "display_name": "קורסי בטיחות",
        "description": "מידע על קורסי בטיחות במכללת במבי, כולל הדרכות, הסמכות, תנאי קבלה, מחירים ומועדים.",
    },
    {
        "tool_id": "faq",
        "display_name": "שאלות נפוצות",
        "description": "שאלות ותשובות כלליות שחוזרות באתר מכללת במבי.",
    },
    {
        "tool_id": "about_the_college",
        "display_name": "אודות המכללה",
        "description": "רקע, פעילות, צוות, מתקנים ומידע כללי על מכללת במבי.",
    },
    {
        "tool_id": "directions_to_bambi_college",
        "display_name": "דרכי הגעה למכללה",
        "description": "כתובת, דרכי הגעה, מיקום ומידע לוגיסטי על הגעה למכללת במבי.",
    },
    {
        "tool_id": "contacting_bambi_college",
        "display_name": "יצירת קשר עם מכללת במבי",
        "description": "טלפונים, טפסי יצירת קשר, שעות פעילות ופרטי התקשרות.",
    },
    {
        "tool_id": "laws_and_regulations",
        "display_name": "חוקים ותקנות",
        "description": "מידע על חוקים, תקנות ודרישות רגולטוריות הרלוונטיות לקורסים ולהכשרות.",
    },
]


def latest_raw_dir(base_dir: Path) -> Path:
    if (base_dir / "manifest.json").exists():
        return base_dir
    candidates = [path for path in base_dir.iterdir() if path.is_dir() and (path / "manifest.json").exists()]
    if not candidates:
        raise FileNotFoundError(f"No raw export manifest found under {base_dir}")
    return sorted(candidates)[-1]


def html_to_text(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_page_text(raw: dict) -> str:
    title_html = raw.get("title", {}).get("rendered", "") if isinstance(raw.get("title"), dict) else ""
    content_html = raw.get("content", {}).get("rendered", "") if isinstance(raw.get("content"), dict) else ""
    excerpt_html = raw.get("excerpt", {}).get("rendered", "") if isinstance(raw.get("excerpt"), dict) else ""

    sections = [
        ("כותרת", html_to_text(title_html)),
        ("תוכן העמוד", html_to_text(content_html)),
        ("תקציר", html_to_text(excerpt_html)),
    ]
    body = "\n\n".join(f"{label}:\n{text}" for label, text in sections if text)
    return body[:MAX_PAGE_TEXT_CHARS]


def normalize_tool_id(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = "knowledge_category"
    if value[0].isdigit():
        value = f"tool_{value}"
    return value[:64]


def load_manifest(raw_dir: Path) -> list[dict]:
    manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    return [item for item in manifest["files"] if item.get("type") in {"pages", "posts"}]


def load_raw_record(raw_dir: Path, item: dict) -> dict:
    return json.loads((raw_dir / item["file"]).read_text(encoding="utf-8"))


def raw_record_exists(raw_dir: Path, item: dict) -> bool:
    return (raw_dir / item["file"]).exists()


def seed_categories() -> dict[str, CategoryState]:
    categories: dict[str, CategoryState] = {}
    for item in SEED_CATEGORIES:
        tool_id = normalize_tool_id(item["tool_id"])
        categories[tool_id] = CategoryState(
            tool_id=tool_id,
            display_name=item["display_name"],
            description=item["description"],
            file_name=f"{GENERATED_DIR_NAME}/{tool_id}.txt",
        )
    return categories


def category_list_for_prompt(categories: dict[str, CategoryState]) -> str:
    lines = []
    for category in categories.values():
        lines.append(
            f"- tool_id: {category.tool_id}\n"
            f"  display_name: {category.display_name}\n"
            f"  description: {category.description}\n"
            f"  source_count: {category.source_count}"
        )
    return "\n".join(lines) or "No categories yet."


def build_prompt(item: dict, page_text: str, categories: dict[str, CategoryState], existing_content: str | None = None) -> str:
    existing_block = existing_content or "אין עדיין מסמך קיים לקטגוריה שנבחרה."
    return f"""
אתה בונה knowledge tools עבור צ'אטבוט של מכללת במבי.
המטרה: להפוך תוכן עמוד WordPress למסמך ידע תמציתי וברור לסוכן.

כללי עבודה:
1. השתמש רק בתוכן העמוד שסופק. אל תנחש ואל תוסיף מידע חיצוני.
2. אם העמוד לא מועיל לבוט, החזר has_bot_value=false ו-category_action=skip.
3. אם יש קטגוריה קיימת מתאימה, בחר אותה ועדכן את summary_document כך שיכלול את המסמך הקיים + המידע החדש, בלי כפילויות.
4. אם אין קטגוריה מתאימה, צור category חדשה עם tool_id באנגלית snake_case, display_name בעברית, ו-tool_description בעברית.
5. המסמך לסוכן חייב להיות בעברית, מאורגן בכותרות קצרות, ולהכיל פרטים שימושיים כמו תנאי קבלה, משך, מחירים, מועדים, קישורים, כתובת, דרכי קשר או FAQ רק אם הם מופיעים בתוכן.
6. אל תשמור רעשי אתר, טקסטים של כפתורים חוזרים, פירורי ניווט, טפסים ריקים, JavaScript, CSS או מידע לא רלוונטי.
7. tool_description צריך להסביר מתי הסוכן צריך להשתמש בכלי הזה.

קטגוריות קיימות:
{category_list_for_prompt(categories)}

מטא־דאטה של העמוד:
type: {item.get("type")}
id: {item.get("id")}
title: {item.get("title")}
link: {item.get("link")}
modified_gmt: {item.get("modified_gmt")}
raw_file: {item.get("file")}

מסמך קיים בקטגוריה שנבחרה, אם רלוונטי:
{existing_block[:MAX_CATEGORY_CONTENT_CHARS]}

תוכן העמוד הנקי:
{page_text}
""".strip()


def call_llm(client: OpenAI, model: str, prompt: str, reasoning_effort: str) -> KnowledgeBuildDecision:
    response = client.responses.parse(
        model=model,
        instructions=(
            "Return only the structured decision. Keep Hebrew summaries factual, concise, and useful for a customer service bot."
        ),
        input=prompt,
        text_format=KnowledgeBuildDecision,
        reasoning={"effort": reasoning_effort},
        max_output_tokens=6000,
    )
    return response.output_parsed


def choose_existing_content(categories: dict[str, CategoryState], decision: KnowledgeBuildDecision) -> str | None:
    if decision.category_action != "existing" or not decision.category_tool_id:
        return None
    category = categories.get(normalize_tool_id(decision.category_tool_id))
    return category.content if category else None


def apply_decision(categories: dict[str, CategoryState], item: dict, decision: KnowledgeBuildDecision) -> None:
    if not decision.has_bot_value or decision.category_action == "skip":
        return

    tool_id = normalize_tool_id(decision.category_tool_id or "")
    if not tool_id:
        raise ValueError(f"LLM returned missing tool_id for useful page {item.get('file')}")

    if tool_id not in categories:
        categories[tool_id] = CategoryState(
            tool_id=tool_id,
            display_name=decision.display_name or tool_id,
            description=decision.tool_description or f"מידע בנושא {decision.display_name or tool_id}.",
            file_name=f"{GENERATED_DIR_NAME}/{tool_id}.txt",
        )

    category = categories[tool_id]
    if decision.display_name:
        category.display_name = decision.display_name
    if decision.tool_description:
        category.description = decision.tool_description
    category.content = decision.summary_document.strip()
    category.source_count += 1
    category.source_files.append(str(item.get("file")))


def write_outputs(categories: dict[str, CategoryState], tools_dir: Path, raw_dir: Path, processed: list[dict], skipped: list[dict]) -> None:
    generated_dir = tools_dir / GENERATED_DIR_NAME
    if generated_dir.exists():
        shutil.rmtree(generated_dir)
    generated_dir.mkdir(parents=True, exist_ok=True)

    tools = []
    for category in categories.values():
        if not category.content.strip():
            continue
        path = tools_dir / category.file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# {category.display_name}\n\n"
            f"תיאור כלי: {category.description}\n\n"
            f"מספר מקורות: {category.source_count}\n\n"
            "## תוכן מסוכם לסוכן\n\n"
        )
        path.write_text(header + category.content.strip() + "\n", encoding="utf-8")
        tools.append(
            {
                "tool_id": category.tool_id,
                "display_name": category.display_name,
                "description": category.description,
                "file_name": category.file_name,
                "source_count": category.source_count,
                "source_files": category.source_files,
            }
        )

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "raw_dir": str(raw_dir),
        "generator": "scripts/build_knowledge_tools.py",
        "tools": tools,
        "processed": processed,
        "skipped": skipped,
    }
    (tools_dir / GENERATED_MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dynamic Bambi knowledge tool files from raw WordPress exports.")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Raw export dir or parent dir containing timestamped exports.")
    parser.add_argument("--tools-dir", default=str(DEFAULT_TOOLS_DIR))
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.5"))
    parser.add_argument("--reasoning-effort", default=os.getenv("OPENAI_REASONING_EFFORT", "low"))
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--max-documents", type=int, default=0, help="Limit documents for testing. 0 means all.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    raw_dir = latest_raw_dir(Path(args.raw_dir))
    tools_dir = Path(args.tools_dir)
    items = load_manifest(raw_dir)
    if args.max_documents:
        items = items[: args.max_documents]

    categories = seed_categories()
    processed: list[dict] = []
    skipped: list[dict] = []

    client = OpenAI()
    for index, item in enumerate(items, start=1):
        if not raw_record_exists(raw_dir, item):
            skipped.append({"file": item.get("file"), "reason": "raw file missing"})
            print(f"[{index}/{len(items)}] skip missing {item.get('file')}", flush=True)
            continue

        raw = load_raw_record(raw_dir, item)
        page_text = extract_page_text(raw)
        if not page_text.strip():
            skipped.append({"file": item.get("file"), "reason": "empty extracted page text"})
            continue

        first_prompt = build_prompt(item, page_text, categories)
        print(f"[{index}/{len(items)}] classify {item.get('type')} {item.get('id')} {item.get('title')}", flush=True)
        decision = call_llm(client, args.model, first_prompt, args.reasoning_effort)

        existing_content = choose_existing_content(categories, decision)
        if existing_content:
            second_prompt = build_prompt(item, page_text, categories, existing_content)
            decision = call_llm(client, args.model, second_prompt, args.reasoning_effort)

        if args.dry_run:
            print(decision.model_dump_json(indent=2), flush=True)
        else:
            apply_decision(categories, item, decision)

        record = {
            "file": item.get("file"),
            "id": item.get("id"),
            "title": item.get("title"),
            "link": item.get("link"),
            "has_bot_value": decision.has_bot_value,
            "category_action": decision.category_action,
            "category_tool_id": decision.category_tool_id,
            "reason": decision.reason,
            "confidence": decision.confidence,
        }
        if decision.has_bot_value and decision.category_action != "skip":
            processed.append(record)
        else:
            skipped.append(record)

        time.sleep(args.sleep)

    if not args.dry_run:
        write_outputs(categories, tools_dir, raw_dir, processed, skipped)
        print(f"Wrote dynamic tools to {tools_dir / GENERATED_DIR_NAME}", flush=True)
        print(f"Wrote manifest to {tools_dir / GENERATED_MANIFEST_NAME}", flush=True)
    else:
        print("Dry run complete; no files written.", flush=True)


if __name__ == "__main__":
    main()
