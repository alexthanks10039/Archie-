from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BOOK_URL = (
    "https://ifreedom.su/ranobe/"
    "ohota-demonicheskogo-korolya-na-svoju-zhenu-"
    "buntujushhaya-ni-na-chto-ne-godnaya-miss-2/"
)
DEFAULT_START = 1590
DEFAULT_END = 2362
DEFAULT_DELAY = 1.0
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 25

CHAPTER_PATTERNS = (
    re.compile(r"(?:/|-)glava[-_ ]?(\d+)(?:/|$)", re.I),
    re.compile(r"(?:/|-)chapter[-_ ]?(\d+)(?:/|$)", re.I),
    re.compile(r"(?:глава|chapter)[\s#№-]*(\d+)", re.I),
)


def normalize_url(base: str, href: str) -> str:
    url = urljoin(base, href)
    url, _ = urldefrag(url)
    return url.rstrip("/") + "/"


def same_domain(url: str, source: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(source).netloc.lower()


def fetch(session: requests.Session, url: str, *, timeout: int, retries: int, delay: float) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            if delay:
                time.sleep(delay)
            return response
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2.0 * attempt, 5.0))
    assert last_error is not None
    raise last_error


def chapter_number_from_text(text: str) -> int | None:
    if not text:
        return None
    for pattern in CHAPTER_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def chapter_numbers_from_anchor(anchor) -> list[int]:
    """
    Return every chapter number explicitly represented by the anchor label.

    iFreedom sometimes publishes a single page under a title such as
    "Глава 1854-1855", while the URL itself contains only "glava-1855".
    The old parser returned only 1855 and therefore falsely reported 1854
    as missing. Prefer the visible range in the anchor text, then fall back
    to the URL.
    """
    href = anchor.get("href", "")
    text = anchor.get_text(" ", strip=True)

    range_match = re.search(
        r"(?:глава|chapter)\s*№?\s*(\d+)\s*[-–—]\s*(\d+)",
        text,
        re.I,
    )
    if range_match:
        first = int(range_match.group(1))
        second = int(range_match.group(2))
        if first <= second:
            return list(range(first, second + 1))
        return list(range(second, first + 1))

    number = chapter_number_from_text(text)
    if number is not None:
        return [number]

    number = chapter_number_from_text(href)
    return [number] if number is not None else []


def likely_chapter_anchor(anchor) -> bool:
    href = anchor.get("href", "")
    text = anchor.get_text(" ", strip=True)
    combined = f"{href} {text}".lower()
    if chapter_number_from_text(href) is not None:
        return True
    return bool(
        re.search(r"глава\s*№?\s*\d+", combined, re.I)
        or re.search(r"chapter\s*#?\s*\d+", combined, re.I)
        or re.search(r"/(?:glava|chapter)[-_ ]?\d+", combined, re.I)
    )


def looks_like_pagination(anchor) -> bool:
    href = anchor.get("href", "")
    text = anchor.get_text(" ", strip=True).lower()
    href_low = href.lower()
    markers = (
        "page=", "/page/", "paged=", "?paged=", "next", "след",
        "стар", "страниц", "older", "новее",
    )
    return any(marker in href_low or marker in text for marker in markers)


def collect_chapter_links(
    session: requests.Session,
    book_url: str,
    start: int,
    end: int,
    *,
    timeout: int,
    retries: int,
    delay: float,
    max_pages: int = 100,
) -> dict[int, str]:
    pending = [normalize_url(book_url, book_url)]
    seen_pages: set[str] = set()
    chapters: dict[int, str] = {}
    base_path = urlparse(book_url).path.rstrip("/")

    while pending and len(seen_pages) < max_pages:
        page_url = pending.pop(0)
        page_url = normalize_url(book_url, page_url)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)

        try:
            response = fetch(
                session, page_url,
                timeout=timeout, retries=retries, delay=delay,
            )
        except requests.RequestException as exc:
            print(f"[WARN] index page failed: {page_url} :: {exc}")
            continue

        soup = BeautifulSoup(response.text, "lxml")

        for anchor in soup.find_all("a", href=True):
            href = normalize_url(page_url, anchor["href"])
            if not same_domain(href, book_url):
                continue

            anchor_path = urlparse(href).path.rstrip("/")
            if base_path and not (
                anchor_path.startswith(base_path) or likely_chapter_anchor(anchor)
            ):
                continue

            if likely_chapter_anchor(anchor):
                for number in chapter_numbers_from_anchor(anchor):
                    if start <= number <= end:
                        chapters.setdefault(number, href)
                continue

            if looks_like_pagination(anchor):
                if href not in seen_pages and href not in pending:
                    pending.append(href)

        for link in soup.find_all("link", href=True):
            rel = link.get("rel") or []
            if isinstance(rel, str):
                rel = [rel]
            if "next" in rel:
                candidate = normalize_url(page_url, link["href"])
                if candidate not in seen_pages and candidate not in pending:
                    pending.append(candidate)

    return dict(sorted(chapters.items()))


def extract_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup([
        "script", "style", "noscript", "iframe", "svg", "form",
        "button", "nav", "header", "footer", "aside",
    ]):
        tag.decompose()

    root = None
    for selector in (
        "article", ".entry-content", ".post-content", ".entry",
        ".td-post-content", ".single-post-content", "main",
    ):
        root = soup.select_one(selector)
        if root:
            break

    if root is None:
        root = soup.body or soup

    for selector in (
        ".sidebar", ".widget", ".comments", ".related-posts",
        ".post-navigation", ".share-buttons", ".navigation",
        ".breadcrumbs", ".breadcrumb",
    ):
        for tag in root.select(selector):
            tag.decompose()

    title = ""
    title_node = soup.find("h1") or soup.find("title")
    if title_node:
        title = title_node.get_text(" ", strip=True)

    blocks: list[str] = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "p", "blockquote", "li"]):
        value = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if value:
            blocks.append(value)

    cleaned: list[str] = []
    for block in blocks:
        if not cleaned or cleaned[-1] != block:
            cleaned.append(block)

    return title, "\n\n".join(cleaned).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"chapters": {}, "missing": [], "errors": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"chapters": {}, "missing": [], "errors": []}


def save_manifest(path: Path, manifest: dict) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_chapter(
    session: requests.Session,
    number: int,
    url: str,
    chapter_dir: Path,
    *,
    timeout: int,
    retries: int,
    delay: float,
) -> dict:
    response = fetch(
        session, url,
        timeout=timeout, retries=retries, delay=delay,
    )
    title, text = extract_text(response.text)

    if len(text) < 80:
        raise ValueError("page parsed, but extracted text is suspiciously short")

    output = chapter_dir / f"{number}.txt"
    output.write_text(f"{title}\n\n{text}\n", encoding="utf-8")

    return {
        "number": number,
        "url": url,
        "final_url": response.url,
        "status": response.status_code,
        "title": title,
        "bytes": output.stat().st_size,
        "sha256": sha256(output),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "file": str(output.as_posix()),
    }


def build_combined_backup(chapter_dir: Path, output: Path, start: int, end: int) -> None:
    chunks: list[str] = []
    for number in range(start, end + 1):
        path = chapter_dir / f"{number}.txt"
        if not path.exists():
            continue
        chunks.append(f"\n\n===== ГЛАВА {number} =====\n\n")
        chunks.append(path.read_text(encoding="utf-8"))
    output.write_text("".join(chunks).lstrip(), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download publicly accessible chapters from iFreedom into a local backup."
    )
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--end", type=int, default=DEFAULT_END)
    parser.add_argument("--output", type=Path, default=Path("backup"))
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--book-url", default=BOOK_URL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start > args.end:
        print("--start must be <= --end", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    chapter_dir = args.output / "chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = args.output / "manifest.json"
    manifest = load_manifest(manifest_path)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })

    print(f"Источник: {args.book_url}")
    print(f"Диапазон: {args.start}-{args.end}")
    print("Собираю ссылки...")

    try:
        links = collect_chapter_links(
            session, args.book_url, args.start, args.end,
            timeout=args.timeout, retries=args.retries,
            delay=args.delay, max_pages=args.max_pages,
        )
    except requests.RequestException as exc:
        print(f"Не удалось открыть книгу: {exc}", file=sys.stderr)
        return 1

    print(f"Найдено глав: {len(links)}")

    manifest.setdefault("chapters", {})
    manifest.setdefault("missing", [])
    manifest.setdefault("errors", [])

    missing = []
    errors = []

    for number in range(args.start, args.end + 1):
        target = chapter_dir / f"{number}.txt"
        if target.exists() and target.stat().st_size > 80:
            print(f"[SKIP] {number}")
            continue

        url = links.get(number)
        if not url:
            print(f"[MISS] {number}")
            missing.append(number)
            continue

        try:
            print(f"[GET ] {number} {url}")
            record = save_chapter(
                session, number, url, chapter_dir,
                timeout=args.timeout, retries=args.retries, delay=args.delay,
            )
            manifest["chapters"][str(number)] = record
            save_manifest(manifest_path, manifest)
        except (requests.RequestException, ValueError, OSError) as exc:
            print(f"[ERR ] {number} :: {exc}")
            errors.append({"number": number, "url": url, "error": str(exc)})

    manifest["missing"] = sorted(set(missing))
    manifest["errors"] = errors
    manifest["range"] = {"start": args.start, "end": args.end}
    manifest["source"] = args.book_url
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(manifest_path, manifest)

    build_combined_backup(
        chapter_dir, args.output / "backup.txt",
        args.start, args.end,
    )

    saved = sum(
        1
        for number in range(args.start, args.end + 1)
        if (chapter_dir / f"{number}.txt").exists()
    )
    print(f"Готово. Сохранено файлов: {saved}")
    print(f"Нет ссылок: {len(manifest['missing'])}")
    print(f"Ошибок: {len(manifest['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
