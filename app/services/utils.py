from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


WHITESPACE_RE = re.compile(r"\s+")
PRICE_RE = re.compile(r"(\d[\d,.\s]{1,20}\s*(?:ש\"ח|₪|שח))")
DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_whitespace(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip()


def clean_html_to_text(html: str) -> tuple[str, list[tuple[str | None, str]]]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = normalize_whitespace(soup.title.text if soup.title else "")
    chunks: list[tuple[str | None, str]] = []

    for node in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        text = normalize_whitespace(node.get_text(" ", strip=True))
        if not text:
            continue
        section = None
        if node.name in {"h1", "h2", "h3"}:
            section = text
        else:
            previous = node.find_previous(["h1", "h2", "h3"])
            if previous:
                section = normalize_whitespace(previous.get_text(" ", strip=True))
        chunks.append((section, text))

    raw_text = "\n".join(text for _, text in chunks)
    return (title, chunks if chunks else [(None, normalize_whitespace(soup.get_text(" ", strip=True)))])


def chunk_text(items: list[tuple[str | None, str]], max_chars: int = 1200) -> list[dict[str, str | None]]:
    chunks: list[dict[str, str | None]] = []
    current_section: str | None = None
    current_parts: list[str] = []
    current_len = 0

    for section, text in items:
        if current_parts and current_len + len(text) > max_chars:
            chunks.append({"section_heading": current_section, "content": "\n".join(current_parts)})
            current_section = section
            current_parts = [text]
            current_len = len(text)
            continue
        if not current_parts:
            current_section = section
        current_parts.append(text)
        current_len += len(text)

    if current_parts:
        chunks.append({"section_heading": current_section, "content": "\n".join(current_parts)})
    return chunks


def collect_internal_links(html: str, base_url: str, allowed_host: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc != allowed_host:
            continue
        clean = href.split("#", 1)[0]
        links.append(clean)
    return list(dict.fromkeys(links))


def slugify_hebrew_fallback(text: str) -> str:
    cleaned = re.sub(r"[^\w\u0590-\u05fe]+", "-", text.lower(), flags=re.UNICODE).strip("-")
    return cleaned or "unknown"


def detect_prices(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(1).strip() for match in PRICE_RE.finditer(text)))


def detect_dates(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in DATE_RE.finditer(text)))
