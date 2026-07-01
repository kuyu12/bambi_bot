from __future__ import annotations

import argparse
import hashlib
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
CHANGE_STATUS_NEW = "new"
CHANGE_STATUS_CHANGED = "changed"
CHANGE_STATUS_UNCHANGED = "unchanged"


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def safe_name(value: str, fallback: str) -> str:
    value = re.sub(r"[^\w\-.]+", "-", value.strip(), flags=re.UNICODE).strip("-")
    return value[:120] or fallback


def canonical_json_hash(payload: Any) -> str:
    """Stable hash for deciding whether a WordPress record changed."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def latest_manifest_dir(base_dir: Path, exclude: Path | None = None) -> Path | None:
    if not base_dir.exists():
        return None

    if (base_dir / "manifest.json").exists():
        candidate = base_dir.resolve()
        if exclude is None or candidate != exclude.resolve():
            return candidate

    candidates = []
    for path in base_dir.iterdir():
        if path.is_dir() and (path / "manifest.json").exists():
            resolved = path.resolve()
            if exclude is not None and resolved == exclude.resolve():
                continue
            candidates.append(path)
    return sorted(candidates)[-1] if candidates else None


def load_previous_index(previous_dir: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    if previous_dir is None:
        return {}
    manifest_path = previous_dir / "manifest.json"
    if not manifest_path.exists():
        return {}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in manifest.get("files", []):
        route = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        if route and item_id:
            index[(route, item_id)] = item
    return index


def change_status(item_hash: str, previous_item: dict[str, Any] | None, modified_gmt: Any) -> str:
    if previous_item is None:
        return CHANGE_STATUS_NEW
    previous_hash = previous_item.get("content_hash")
    if previous_hash and previous_hash == item_hash:
        return CHANGE_STATUS_UNCHANGED
    if previous_hash:
        return CHANGE_STATUS_CHANGED
    if str(previous_item.get("modified_gmt") or "") == str(modified_gmt or ""):
        return CHANGE_STATUS_UNCHANGED
    return CHANGE_STATUS_CHANGED


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
    previous_index: dict[tuple[str, str], dict[str, Any]],
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
            item_hash = canonical_json_hash(item)
            previous_item = previous_index.get((route, item_id))
            status = change_status(item_hash, previous_item, item.get("modified_gmt"))
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
                    "content_hash": item_hash,
                    "change_status": status,
                    "previous_file": previous_item.get("file") if previous_item else None,
                    "previous_modified_gmt": previous_item.get("modified_gmt") if previous_item else None,
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
    parser.add_argument(
        "--previous-dir",
        default="",
        help="Previous raw export dir for delta detection. Defaults to latest sibling export under the out-dir parent.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    previous_dir = Path(args.previous_dir).resolve() if args.previous_dir else latest_manifest_dir(out_dir.parent, exclude=out_dir)
    previous_index = load_previous_index(previous_dir)

    started_at = datetime.now(UTC).isoformat()
    routes = [item.strip() for item in args.types.split(",") if item.strip()]
    manifest: dict[str, Any] = {
        "base_url": args.base_url,
        "started_at": started_at,
        "user_agent": USER_AGENT,
        "per_page": args.per_page,
        "sleep_seconds": args.sleep,
        "routes": routes,
        "previous_raw_dir": str(previous_dir) if previous_dir else None,
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
            previous_index=previous_index,
        )
        manifest["files"].extend(files)
        manifest["errors"].extend(errors)
        time.sleep(args.sleep)

    changed_files = [
        item
        for item in manifest["files"]
        if item.get("change_status") in {CHANGE_STATUS_NEW, CHANGE_STATUS_CHANGED}
    ]
    manifest["changes"] = {
        "new": sum(1 for item in manifest["files"] if item.get("change_status") == CHANGE_STATUS_NEW),
        "changed": sum(1 for item in manifest["files"] if item.get("change_status") == CHANGE_STATUS_CHANGED),
        "unchanged": sum(1 for item in manifest["files"] if item.get("change_status") == CHANGE_STATUS_UNCHANGED),
        "total_delta": len(changed_files),
    }
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    manifest["total_files"] = len(manifest["files"])
    write_json(out_dir / "manifest.json", manifest)
    write_json(
        out_dir / "changed_files.json",
        {
            "generated_at": manifest["finished_at"],
            "base_url": args.base_url,
            "raw_dir": str(out_dir),
            "previous_raw_dir": str(previous_dir) if previous_dir else None,
            "total_files": len(changed_files),
            "files": changed_files,
        },
    )

    index_path = out_dir / "index.jsonl"
    with index_path.open("w", encoding="utf-8") as handle:
        for item in manifest["files"]:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"Exported {manifest['total_files']} raw WordPress records to {out_dir}", flush=True)
    print(
        "Delta: "
        f"new={manifest['changes']['new']} "
        f"changed={manifest['changes']['changed']} "
        f"unchanged={manifest['changes']['unchanged']}",
        flush=True,
    )
    if manifest["errors"]:
        print(f"Completed with {len(manifest['errors'])} errors. See manifest.json.", flush=True)


if __name__ == "__main__":
    main()
