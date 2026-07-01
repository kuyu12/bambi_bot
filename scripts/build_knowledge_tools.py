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
CHANGED_FILES_MANIFEST_NAME = "changed_files.json"
MAX_CATEGORY_SNAPSHOT_CHARS = 1_200
MAX_PAGE_TEXT_CHARS = 10_000
MAX_SUMMARY_OUTPUT_TOKENS = 3_500
GENERATED_CONTENT_MARKER = "## תוכן מסוכם לסוכן"

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


class NonRetryableLLMError(RuntimeError):
    pass


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
SEED_TOOL_IDS = {item["tool_id"] for item in SEED_CATEGORIES}
NEW_COURSE_TOOL_PREFIX = "course_"


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


def unwrap_generated_content(content: str) -> str:
    """Keep only the knowledge body from generated tool files, even after repeated resume writes."""
    body = content.strip()
    while GENERATED_CONTENT_MARKER in body and body.lstrip().startswith("#"):
        body = body.split(GENERATED_CONTENT_MARKER, 1)[1].strip()
    return body


def normalize_tool_id(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = "knowledge_category"
    if value[0].isdigit():
        value = f"tool_{value}"
    return value[:64]


def is_allowed_tool_id(tool_id: str) -> bool:
    return tool_id in SEED_TOOL_IDS or tool_id.startswith(NEW_COURSE_TOOL_PREFIX)


def load_manifest(raw_dir: Path, *, only_changed: bool = False) -> list[dict]:
    manifest_name = CHANGED_FILES_MANIFEST_NAME if only_changed else "manifest.json"
    manifest_path = raw_dir / manifest_name
    if only_changed and not manifest_path.exists():
        raise FileNotFoundError(f"{manifest_name} not found under {raw_dir}. Run export_wordpress_raw.py first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [item for item in manifest["files"] if item.get("type") in {"pages", "posts"}]


def load_raw_record(raw_dir: Path, item: dict) -> dict:
    return json.loads((raw_dir / item["file"]).read_text(encoding="utf-8"))


def raw_record_exists(raw_dir: Path, item: dict) -> bool:
    return (raw_dir / item["file"]).exists()


def completion_key(item: dict, *, include_hash: bool = False) -> str:
    file_name = str(item.get("file"))
    if not include_hash:
        return file_name
    return f"{file_name}::{item.get('content_hash') or item.get('modified_gmt') or ''}"


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
        snapshot = category.content.strip()[:MAX_CATEGORY_SNAPSHOT_CHARS] or "No existing content yet."
        lines.append(
            f"- tool_id: {category.tool_id}\n"
            f"  display_name: {category.display_name}\n"
            f"  description: {category.description}\n"
            f"  source_count: {category.source_count}\n"
            f"  existing_summary_snapshot: {snapshot}"
        )
    return "\n".join(lines) or "No categories yet."


def build_prompt(item: dict, page_text: str, categories: dict[str, CategoryState]) -> str:
    return f"""
אתה בונה knowledge tools עבור צ'אטבוט של מכללת במבי.
המטרה: להפוך תוכן עמוד WordPress למסמך ידע תמציתי וברור לסוכן.

כללי עבודה:
1. השתמש רק בתוכן העמוד שסופק. אל תנחש ואל תוסיף מידע חיצוני.
2. אם העמוד לא מועיל לבוט, החזר has_bot_value=false ו-category_action=skip.
3. עבור עמוד שעוסק בקורס ספציפי, צור כלי חדש ונפרד לקורס עם category_action=new ו-tool_id באנגלית שמתחיל תמיד ב-course_.
4. עבור עמוד אינדקס או עמוד כללי, השתמש באחת מהקטגוריות הקיימות שמתאימה.
5. אם העמוד לא עוסק בקורס/הכשרה/הסמכה או מידע שירותי חשוב למכללה, החזר has_bot_value=false ו-category_action=skip.
6. אל תיצור tools חדשים לנגישות, תודה, טפסים, המלצות, חדשות כלליות, מאמרים שיווקיים כלליים או עמודים ללא מידע עובדתי על קורס.
7. המסמך לסוכן חייב להיות בעברית, מאורגן בכותרות קצרות, ולהכיל פרטים שימושיים כמו תנאי קבלה, משך, מחירים, מועדים, קישורים, כתובת, דרכי קשר או FAQ רק אם הם מופיעים בתוכן.
8. אל תשמור רעשי אתר, טקסטים של כפתורים חוזרים, פירורי ניווט, טפסים ריקים, JavaScript, CSS או מידע לא רלוונטי.
9. tool_description צריך להסביר מתי הסוכן צריך להשתמש בכלי הזה.
10. summary_document חייב להיות מסמך מרוכז ותמציתי עד 2,500 תווים. אם יש מידע כפול או פחות חשוב, מחק אותו במקום להאריך את המסמך.
11. אל תנסה לשכתב את כל המסמך הקיים בקטגוריה קיימת. המערכת תשמור את התוכן הקיים ותצרף אליו את הסיכום החדש.
12. tool_id חדש לקורס חייב להיות snake_case באנגלית ולהתחיל ב-course_, לדוגמה course_forklift, course_work_at_height, course_mobile_machine.

קטגוריות קיימות:
{category_list_for_prompt(categories)}

מטא־דאטה של העמוד:
type: {item.get("type")}
id: {item.get("id")}
title: {item.get("title")}
link: {item.get("link")}
modified_gmt: {item.get("modified_gmt")}
raw_file: {item.get("file")}

תוכן העמוד הנקי:
{page_text}
""".strip()


def call_llm(client: OpenAI, model: str, prompt: str, reasoning_effort: str, timeout: float) -> KnowledgeBuildDecision:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.responses.parse(
                model=model,
                instructions=(
                    "Return only the structured decision. Keep Hebrew summaries factual, concise, and useful for a customer service bot."
                ),
                input=prompt,
                text_format=KnowledgeBuildDecision,
                reasoning={"effort": reasoning_effort},
                max_output_tokens=MAX_SUMMARY_OUTPUT_TOKENS,
                timeout=timeout,
            )
            return response.output_parsed
        except Exception as exc:  # noqa: BLE001 - batch job should retry parse/API failures.
            last_error = exc
            if "insufficient_quota" in str(exc):
                raise NonRetryableLLMError(f"LLM quota is exhausted: {exc}") from exc
            print(f"LLM call failed on attempt {attempt}/3: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2 * attempt)
    raise RuntimeError(f"LLM call failed after retries: {last_error}") from last_error


def load_existing_state(tools_dir: Path, raw_dir: Path) -> tuple[dict[str, CategoryState], list[dict], list[dict]]:
    manifest_path = tools_dir / GENERATED_MANIFEST_NAME
    if not manifest_path.exists():
        return seed_categories(), [], []

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    categories = seed_categories()
    for item in manifest.get("tools", []):
        tool_id = normalize_tool_id(str(item["tool_id"]))
        if not is_allowed_tool_id(tool_id):
            continue
        file_name = str(item["file_name"])
        content_path = tools_dir / file_name
        content = unwrap_generated_content(content_path.read_text(encoding="utf-8")) if content_path.exists() else ""
        categories[tool_id] = CategoryState(
            tool_id=tool_id,
            display_name=str(item.get("display_name") or tool_id),
            description=str(item.get("description") or ""),
            file_name=file_name,
            content=content,
            source_count=int(item.get("source_count") or 0),
            source_files=[str(value) for value in item.get("source_files", [])],
        )

    processed = list(manifest.get("processed", []))
    skipped = list(manifest.get("skipped", []))
    print(
        f"Resuming from {manifest_path}: done={len(processed) + len(skipped)} "
        f"processed={len(processed)} skipped={len(skipped)} tools={len(manifest.get('tools', []))}",
        flush=True,
    )
    return categories, processed, skipped


def apply_decision(categories: dict[str, CategoryState], item: dict, decision: KnowledgeBuildDecision) -> None:
    if not decision.has_bot_value or decision.category_action == "skip":
        return

    tool_id = normalize_tool_id(decision.category_tool_id or "")
    if not tool_id:
        raise ValueError(f"LLM returned missing tool_id for useful page {item.get('file')}")
    if not is_allowed_tool_id(tool_id):
        return

    has_existing_content = tool_id in categories and bool(categories[tool_id].content.strip())
    if tool_id not in categories:
        categories[tool_id] = CategoryState(
            tool_id=tool_id,
            display_name=decision.display_name or tool_id,
            description=decision.tool_description or f"מידע על {decision.display_name or tool_id}.",
            file_name=f"{GENERATED_DIR_NAME}/{tool_id}.txt",
        )

    category = categories[tool_id]
    if decision.display_name:
        category.display_name = decision.display_name
    if decision.tool_description:
        category.description = decision.tool_description
    new_summary = decision.summary_document.strip()
    if has_existing_content and decision.category_action == "existing":
        source_title = html_to_text(str(item.get("title") or "")).strip() or str(item.get("file") or "מקור נוסף")
        source_link = str(item.get("link") or "").strip()
        source_header = f"## מידע נוסף: {source_title}"
        if source_link:
            source_header += f"\nמקור: {source_link}"
        category.content = f"{category.content.rstrip()}\n\n{source_header}\n\n{new_summary}".strip()
    else:
        category.content = new_summary
    category.source_count += 1
    category.source_files.append(str(item.get("file")))


def constrain_decision_to_allowed_tools(decision: KnowledgeBuildDecision) -> KnowledgeBuildDecision:
    if not decision.has_bot_value or decision.category_action == "skip":
        return decision
    tool_id = normalize_tool_id(decision.category_tool_id or "")
    if not is_allowed_tool_id(tool_id):
        decision.has_bot_value = False
        decision.category_action = "skip"
        decision.category_tool_id = None
        decision.summary_document = ""
        decision.reason = f"{decision.reason} | skipped because new dynamic tools must be course-specific and start with {NEW_COURSE_TOOL_PREFIX}"
        return decision
    if decision.category_action == "new" and tool_id in SEED_TOOL_IDS:
        decision.category_action = "existing"
    decision.category_tool_id = tool_id
    return decision


def write_outputs(categories: dict[str, CategoryState], tools_dir: Path, raw_dir: Path, processed: list[dict], skipped: list[dict]) -> None:
    generated_dir = tools_dir / GENERATED_DIR_NAME
    if generated_dir.exists():
        shutil.rmtree(generated_dir)
    generated_dir.mkdir(parents=True, exist_ok=True)

    tools = []
    for category in categories.values():
        is_seed_category = category.tool_id in SEED_TOOL_IDS
        if not category.content.strip() and not is_seed_category:
            continue
        path = tools_dir / category.file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = unwrap_generated_content(category.content)
        if not body:
            body = "אין מידע מאומת עדיין עבור כלי זה. אם המשתמש שואל בנושא זה, יש להשיב שחסר מידע ולבקש בדיקה אנושית."
        header = (
            f"# {category.display_name}\n\n"
            f"תיאור כלי: {category.description}\n\n"
            f"מספר מקורות: {category.source_count}\n\n"
            "## תוכן מסוכם לסוכן\n\n"
        )
        path.write_text(header + body + "\n", encoding="utf-8")
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
    parser.add_argument("--llm-timeout", type=float, default=120.0)
    parser.add_argument("--max-documents", type=int, default=0, help="Limit documents for testing. 0 means all.")
    parser.add_argument("--checkpoint-every", type=int, default=1, help="Write generated tool files every N processed documents.")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--only-changed",
        action="store_true",
        help=f"Process only files listed in {CHANGED_FILES_MANIFEST_NAME} from the selected raw export.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    raw_dir = latest_raw_dir(Path(args.raw_dir))
    tools_dir = Path(args.tools_dir)
    items = load_manifest(raw_dir, only_changed=args.only_changed)
    if args.only_changed:
        print(f"Processing only changed/new raw files from {raw_dir / CHANGED_FILES_MANIFEST_NAME}: {len(items)} documents", flush=True)
    if args.max_documents:
        items = items[: args.max_documents]

    if args.resume and not args.dry_run:
        categories, processed, skipped = load_existing_state(tools_dir, raw_dir)
    else:
        categories = seed_categories()
        processed = []
        skipped = []
    completed_files = {completion_key(item, include_hash=args.only_changed) for item in processed + skipped}

    client = OpenAI()
    for index, item in enumerate(items, start=1):
        item_completion_key = completion_key(item, include_hash=args.only_changed)
        if item_completion_key in completed_files:
            print(f"[{index}/{len(items)}] resume skip already done {item.get('file')}", flush=True)
            continue

        if not raw_record_exists(raw_dir, item):
            record = {"file": item.get("file"), "reason": "raw file missing"}
            skipped.append(record)
            completed_files.add(item_completion_key)
            print(f"[{index}/{len(items)}] skip missing {item.get('file')}", flush=True)
            continue

        raw = load_raw_record(raw_dir, item)
        page_text = extract_page_text(raw)
        if not page_text.strip():
            skipped.append({"file": item.get("file"), "reason": "empty extracted page text"})
            completed_files.add(item_completion_key)
            continue

        first_prompt = build_prompt(item, page_text, categories)
        print(f"[{index}/{len(items)}] classify {item.get('type')} {item.get('id')} {item.get('title')}", flush=True)
        try:
            decision = call_llm(client, args.model, first_prompt, args.reasoning_effort, args.llm_timeout)
            decision = constrain_decision_to_allowed_tools(decision)
        except NonRetryableLLMError as exc:
            print(f"Stopping build without marking current file as skipped: {exc}", flush=True)
            if not args.dry_run:
                write_outputs(categories, tools_dir, raw_dir, processed, skipped)
            raise SystemExit(2) from exc
        except Exception as exc:  # noqa: BLE001 - continue the offline batch and record the failure.
            record = {
                "file": item.get("file"),
                "id": item.get("id"),
                "title": item.get("title"),
                "link": item.get("link"),
                "reason": f"llm_error: {type(exc).__name__}: {exc}",
            }
            skipped.append(record)
            completed_files.add(item_completion_key)
            if not args.dry_run:
                write_outputs(categories, tools_dir, raw_dir, processed, skipped)
            continue

        if args.dry_run:
            print(decision.model_dump_json(indent=2), flush=True)
        else:
            apply_decision(categories, item, decision)

        record = {
            "file": item.get("file"),
            "id": item.get("id"),
            "title": item.get("title"),
            "link": item.get("link"),
            "content_hash": item.get("content_hash"),
            "change_status": item.get("change_status"),
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
        completed_files.add(item_completion_key)

        if not args.dry_run and args.checkpoint_every > 0 and (len(processed) + len(skipped)) % args.checkpoint_every == 0:
            write_outputs(categories, tools_dir, raw_dir, processed, skipped)
            print(
                f"Checkpoint written: processed={len(processed)} skipped={len(skipped)} tools={sum(1 for c in categories.values() if c.content.strip())}",
                flush=True,
            )

        time.sleep(args.sleep)

    if not args.dry_run:
        write_outputs(categories, tools_dir, raw_dir, processed, skipped)
        print(f"Wrote dynamic tools to {tools_dir / GENERATED_DIR_NAME}", flush=True)
        print(f"Wrote manifest to {tools_dir / GENERATED_MANIFEST_NAME}", flush=True)
    else:
        print("Dry run complete; no files written.", flush=True)


if __name__ == "__main__":
    main()
