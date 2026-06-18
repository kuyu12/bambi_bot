from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


USER_AGENT = "BambiKnowledgeBot/1.0"
DEFAULT_TYPES = ("pages", "posts")


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def safe_name(value: str, fallback: str) -> str:
    value = re.sub(r"[^\w\-.]+", "-", value.strip(), flags=re.UNICODE).strip("-")
    return value[:120] or fallback


def fetch_json(url: str, timeout: int) -> tuple[Any, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        headers = {key.lower(): value for key, value in response.headers.items()}
        return json.loads(body), headers


def fetch_text(url: str, timeout: int) -> tuple[str, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        headers = {key.lower(): value for key, value in response.headers.items()}
        return body, headers


def build_url(base_url: str, route: str, params: dict[str, str | int]) -> str:
    query = urllib.parse.urlencode(params)
    return f"{base_url.rstrip('/')}/wp-json/wp/v2/{route}?{query}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def export_collection(
    *,
    base_url: str,
    route: str,
    out_dir: Path,
    per_page: int,
    sleep_seconds: float,
    timeout: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        url = build_url(
            base_url,
            route,
            {
                "per_page": per_page,
                "page": page,
                "orderby": "modified",
                "order": "desc",
                "_embed": 1,
            },
        )
        print(f"[{route}] fetching page {page}: {url}", flush=True)
        try:
            payload, headers = fetch_json(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and page > 1:
                break
            errors.append({"route": route, "page": str(page), "url": url, "error": f"HTTP {exc.code}: {exc.reason}"})
            break
        except Exception as exc:  # noqa: BLE001 - exporter should record and continue where possible.
            errors.append({"route": route, "page": str(page), "url": url, "error": repr(exc)})
            break

        if total_pages is None:
            total_pages = int(headers.get("x-wp-totalpages", "1") or "1")
            print(f"[{route}] total pages: {total_pages}", flush=True)

        if not isinstance(payload, list):
            errors.append({"route": route, "page": str(page), "url": url, "error": "Expected list payload"})
            break

        for item in payload:
            item_id = str(item.get("id", "unknown"))
            slug = safe_name(str(item.get("slug") or ""), f"{route}-{item_id}")
            file_path = out_dir / "raw" / route / f"{item_id}-{slug}.json"
            write_json(file_path, item)
            title = item.get("title", {}).get("rendered") if isinstance(item.get("title"), dict) else ""
            files.append(
                {
                    "type": route,
                    "id": item.get("id"),
                    "slug": item.get("slug"),
                    "title": title,
                    "link": item.get("link"),
                    "date_gmt": item.get("date_gmt"),
                    "modified_gmt": item.get("modified_gmt"),
                    "file": str(file_path.relative_to(out_dir)).replace("\\", "/"),
                }
            )

        page += 1
        if page <= (total_pages or 0):
            time.sleep(sleep_seconds)

    return files, errors


def export_auxiliary_files(base_url: str, out_dir: Path, timeout: int, sleep_seconds: float) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for name, path in {
        "wp_index": "/wp-json/",
        "sitemap_index": "/sitemap_index.xml",
        "feed": "/feed/",
        "robots": "/robots.txt",
    }.items():
        url = base_url.rstrip("/") + path
        print(f"[aux] fetching {url}", flush=True)
        try:
            body, headers = fetch_text(url, timeout)
            suffix = ".json" if "json" in (headers.get("content-type") or "") else ".xml" if "xml" in (headers.get("content-type") or "") else ".txt"
            file_path = out_dir / "raw" / "auxiliary" / f"{name}{suffix}"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(body, encoding="utf-8")
            results.append({"name": name, "url": url, "file": str(file_path.relative_to(out_dir)).replace("\\", "/")})
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "url": url, "error": repr(exc)})
        time.sleep(sleep_seconds)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Export raw WordPress content for the Bambi knowledge pipeline.")
    parser.add_argument("--base-url", default="https://bambischool.co.il")
    parser.add_argument("--out-dir", default=f"data/website_raw/{utc_stamp()}")
    parser.add_argument("--types", default=",".join(DEFAULT_TYPES), help="Comma-separated WordPress routes, e.g. pages,posts")
    parser.add_argument("--per-page", type=int, default=25)
    parser.add_argument("--sleep", type=float, default=1.5)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(UTC).isoformat()
    routes = [item.strip() for item in args.types.split(",") if item.strip()]
    manifest: dict[str, Any] = {
        "base_url": args.base_url,
        "started_at": started_at,
        "user_agent": USER_AGENT,
        "per_page": args.per_page,
        "sleep_seconds": args.sleep,
        "routes": routes,
        "files": [],
        "auxiliary": [],
        "errors": [],
    }

    manifest["auxiliary"] = export_auxiliary_files(args.base_url, out_dir, args.timeout, args.sleep)

    for route in routes:
        files, errors = export_collection(
            base_url=args.base_url,
            route=route,
            out_dir=out_dir,
            per_page=args.per_page,
            sleep_seconds=args.sleep,
            timeout=args.timeout,
        )
        manifest["files"].extend(files)
        manifest["errors"].extend(errors)
        time.sleep(args.sleep)

    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["total_files"] = len(manifest["files"])
    write_json(out_dir / "manifest.json", manifest)

    index_path = out_dir / "index.jsonl"
    with index_path.open("w", encoding="utf-8") as handle:
        for item in manifest["files"]:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"Exported {manifest['total_files']} raw WordPress records to {out_dir}", flush=True)
    if manifest["errors"]:
        print(f"Completed with {len(manifest['errors'])} errors. See manifest.json.", flush=True)


if __name__ == "__main__":
    main()
