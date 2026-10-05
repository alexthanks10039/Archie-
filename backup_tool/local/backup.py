
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
MIN_CHAPTER_TEXT = 80
MIN_MERGED_CHAPTER_TEXT = 80

CHAPTER_PATTERNS = (
    re.compile(r"(?:/|-)glava[-_ ]?(\d+)(?:/|$)", re.I),
    re.compile(r"(?:/|-)chapter[-_ ]?(\d+)(?:/|$)", re.I),
    re.compile(r"(?:глава|chapter)[\s#№-]*(\d+)", re.I),
)
CHAPTER_HEADING_PATTERN = re.compile(
    r"^(?:глава|chapter)\s*[#№]?\s*(\d+)"
    r"(?:\s*[-–—]\s*(\d+))?"
    r"(?:\s*[:.].*)?$",
    re.I,
)


def normalize_url(base: str, href: str) -> str:
    url = urljoin(base, href)
    url, _ = urldefrag(url)
    return url.rstrip("/") + "/"


def same_domain(url: str, source: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(source).netloc.lower()


def fetch(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    retries: int,
    delay: float,
) -> requests.Response:
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


def chapter_numbers_from_label(text: str) -> list[int]:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return []

    range_match = re.search(
        r"(?:глава|chapter)\s*[#№]?\s*(\d+)\s*[-–—]\s*(\d+)",
        value,
        re.I,
    )
    if range_match:
        first = int(range_match.group(1))
        second = int(range_match.group(2))
        if first <= second:
            return list(range(first, second + 1))
        return list(range(second, first + 1))

    number = chapter_number_from_text(value)
    return [number] if number is not None else []


def chapter_numbers_from_anchor(anchor) -> list[int]:
    """Return all chapter numbers explicitly represented by an anchor label."""
    numbers = chapter_numbers_from_label(anchor.get_text(" ", strip=True))
    if numbers:
        return numbers
    number = chapter_number_from_text(anchor.get("href", ""))
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
    """Return chapter_number -> source URL.

    A merged source maps every represented chapter number to the same source
    URL. Callers must group URLs before downloading.
    """
    pending = [normalize_url(book_url, book_url)]
    seen_pages: set[str] = set()
    chapters: dict[int, str] = {}
    base_path = urlparse(book_url).path.rstrip("/")

    while pending and len(seen_pages) < max_pages:
        page_url = normalize_url(book_url, pending.pop(0))
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)

        try:
            response = fetch(
                session,
                page_url,
                timeout=timeout,
                retries=retries,
                delay=delay,
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


def group_chapter_links(links: dict[int, str]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for number, url in links.items():
        grouped.setdefault(url, []).append(number)
    for numbers in grouped.values():
        numbers.sort()
    return dict(sorted(grouped.items(), key=lambda item: item[1][0]))


def find_content_root(soup: BeautifulSoup):
    for selector in (
        "article", ".entry-content", ".post-content", ".entry",
        ".td-post-content", ".single-post-content", "main",
    ):
        root = soup.select_one(selector)
        if root:
            break
    else:
        root = soup.body or soup

    for selector in (
        ".sidebar", ".widget", ".comments", ".related-posts",
        ".post-navigation", ".share-buttons", ".navigation",
        ".breadcrumbs", ".breadcrumb",
    ):
        for tag in root.select(selector):
            tag.decompose()

    return root


def clean_soup(html: str) -> tuple[BeautifulSoup, BeautifulSoup]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup([
        "script", "style", "noscript", "iframe", "svg", "form",
        "button", "nav", "header", "footer", "aside",
    ]):
        tag.decompose()
    return soup, find_content_root(soup)


def extract_content_blocks(html: str) -> tuple[str, list[tuple[str, str]]]:
    soup, root = clean_soup(html)

    title = ""
    title_node = soup.find("h1") or soup.find("title")
    if title_node:
        title = re.sub(r"\s+", " ", title_node.get_text(" ", strip=True)).strip()

    blocks: list[tuple[str, str]] = []
    for node in root.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li"]
    ):
        value = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if value:
            blocks.append((node.name.lower(), value))

    cleaned: list[tuple[str, str]] = []
    for tag, block in blocks:
        if not cleaned or cleaned[-1][1] != block:
            cleaned.append((tag, block))
    return title, cleaned


def extract_text(html: str) -> tuple[str, str]:
    title, blocks = extract_content_blocks(html)
    return title, "\n\n".join(block for _, block in blocks).strip()


def detect_heading_numbers(text: str) -> list[int]:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    match = CHAPTER_HEADING_PATTERN.match(normalized)
    if not match:
        return []

    first = int(match.group(1))
    second = match.group(2)
    if second:
        second_num = int(second)
        return (
            list(range(first, second_num + 1))
            if first <= second_num
            else list(range(second_num, first + 1))
        )
    return [first]


def split_merged_chapters(
    html: str,
    expected_numbers: list[int],
) -> dict[int, dict[str, str]]:
    """Split a merged source page using explicit chapter headings.

    The function is deliberately conservative. If every expected chapter does
    not have an unambiguous heading in the content, it raises instead of
    duplicating or guessing text.
    """
    expected = sorted(set(expected_numbers))
    if len(expected) < 2:
        raise ValueError("split_merged_chapters requires at least two chapters")

    _, blocks = extract_content_blocks(html)
    if not blocks:
        raise ValueError("content root contains no readable blocks")

    positions: dict[int, int] = {}
    for index, (_, block) in enumerate(blocks):
        numbers = detect_heading_numbers(block)
        if not numbers:
            continue

        # A single heading covering the entire merged range is not enough to
        # establish where the individual chapter texts begin and end.
        if numbers == expected:
            continue

        for number in numbers:
            if number in expected and number not in positions:
                positions[number] = index

    missing = [number for number in expected if number not in positions]
    if missing:
        raise ValueError(
            "individual chapter headings not found for: "
            + ", ".join(map(str, missing))
        )

    ordered = [(number, positions[number]) for number in expected]
    if any(a[1] >= b[1] for a, b in zip(ordered, ordered[1:])):
        raise ValueError("chapter headings are not in ascending document order")

    result: dict[int, dict[str, str]] = {}
    for idx, (number, heading_index) in enumerate(ordered):
        end_index = ordered[idx + 1][1] if idx + 1 < len(ordered) else len(blocks)
        body = "\n\n".join(
            block for _, block in blocks[heading_index + 1:end_index]
        ).strip()

        if len(body) < MIN_MERGED_CHAPTER_TEXT:
            raise ValueError(
                f"chapter {number}: suspiciously short text ({len(body)} chars)"
            )

        result[number] = {
            "title": blocks[heading_index][1],
            "text": body,
        }

    normalized_texts: dict[int, str] = {}
    for number, item in result.items():
        normalized = re.sub(r"\s+", " ", item["text"]).strip()
        duplicate_of = next(
            (other for other, value in normalized_texts.items() if value == normalized),
            None,
        )
        if duplicate_of is not None:
            raise ValueError(
                f"chapter {number} is identical to chapter {duplicate_of}; "
                "refusing to save duplicate merged content"
            )
        normalized_texts[number] = normalized

    return result


def split_merged_page(
    html: str,
    expected_numbers: list[int],
) -> dict[int, dict[str, str]]:
    return split_merged_chapters(html, expected_numbers)


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


def write_chapter_file(
    chapter_dir: Path,
    number: int,
    title: str,
    text: str,
) -> Path:
    if not text or len(text.strip()) < MIN_CHAPTER_TEXT:
        raise ValueError(f"chapter {number}: extracted text is too short")
    output = chapter_dir / f"{number}.txt"
    output.write_text(f"{title}\n\n{text}\n", encoding="utf-8")
    return output


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
        session,
        url,
        timeout=timeout,
        retries=retries,
        delay=delay,
    )
    title, text = extract_text(response.text)
    output = write_chapter_file(chapter_dir, number, title, text)

    return {
        "number": number,
        "url": url,
        "final_url": response.url,
        "status": response.status_code,
        "title": title,
        "bytes": output.stat().st_size,
        "chars": len(text),
        "sha256": sha256(output),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "file": str(output.as_posix()),
        "source_numbers": [number],
    }


def save_merged_chapters(
    html: str,
    source_url: str,
    chapter_numbers: list[int],
    chapter_dir: Path,
) -> dict[int, dict]:
    sections = split_merged_chapters(html, chapter_numbers)
    saved: dict[int, dict] = {}

    for number in chapter_numbers:
        item = sections[number]
        output = write_chapter_file(
            chapter_dir,
            number,
            item["title"],
            item["text"],
        )
        saved[number] = {
            "number": number,
            "url": source_url,
            "final_url": source_url,
            "status": 200,
            "title": item["title"],
            "bytes": output.stat().st_size,
            "chars": len(item["text"]),
            "sha256": sha256(output),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "file": str(output.as_posix()),
            "source_numbers": chapter_numbers,
            "merged": True,
        }
    return saved


def build_combined_backup(
    chapter_dir: Path,
    output: Path,
    start: int,
    end: int,
) -> None:
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
            session,
            args.book_url,
            args.start,
            args.end,
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
            max_pages=args.max_pages,
        )
    except requests.RequestException as exc:
        print(f"Не удалось открыть книгу: {exc}", file=sys.stderr)
        return 1

    grouped = group_chapter_links(links)
    found_numbers = sorted(links)

    print(f"Найдено глав: {len(found_numbers)}")
    print(f"Найдено страниц-источников: {len(grouped)}")

    manifest.setdefault("chapters", {})
    manifest.setdefault("missing", [])
    manifest.setdefault("errors", [])

    missing = [
        number
        for number in range(args.start, args.end + 1)
        if number not in links
    ]
    errors = []

    for source_url, chapter_numbers in grouped.items():
        relevant = [
            number for number in chapter_numbers
            if args.start <= number <= args.end
        ]
        if not relevant:
            continue

        if all(
            (chapter_dir / f"{number}.txt").exists()
            and (chapter_dir / f"{number}.txt").stat().st_size > MIN_CHAPTER_TEXT
            for number in relevant
        ):
            print(f"[SKIP] {relevant} :: {source_url}")
            continue

        try:
            if len(relevant) == 1:
                number = relevant[0]
                print(f"[GET ] {number} {source_url}")
                record = save_chapter(
                    session,
                    number,
                    source_url,
                    chapter_dir,
                    timeout=args.timeout,
                    retries=args.retries,
                    delay=args.delay,
                )
                manifest["chapters"][str(number)] = record
            else:
                print(
                    f"[MERGED] source page: {source_url} -> "
                    f"chapters {', '.join(map(str, relevant))}"
                )
                response = fetch(
                    session,
                    source_url,
                    timeout=args.timeout,
                    retries=args.retries,
                    delay=args.delay,
                )
                records = save_merged_chapters(
                    response.text,
                    response.url,
                    relevant,
                    chapter_dir,
                )
                manifest["chapters"].update(
                    {str(number): record for number, record in records.items()}
                )
                for number, record in records.items():
                    print(
                        f"[MERGED] chapter {number}: {record['chars']} chars"
                    )

            save_manifest(manifest_path, manifest)

        except (requests.RequestException, ValueError, OSError) as exc:
            for number in relevant:
                target = chapter_dir / f"{number}.txt"
                if target.exists() and target.stat().st_size <= MIN_CHAPTER_TEXT:
                    target.unlink()

            error_record = {
                "url": source_url,
                "chapter_numbers": relevant,
                "type": type(exc).__name__,
                "error": str(exc),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            errors.append(error_record)
            manifest["errors"] = errors
            save_manifest(manifest_path, manifest)
            print(f"[ERR ] {relevant} :: {exc}", file=sys.stderr)

    manifest["missing"] = sorted(set(missing))
    manifest["errors"] = errors
    manifest["range"] = {"start": args.start, "end": args.end}
    manifest["source"] = args.book_url
    manifest["stats"] = {
        "expected_chapters": args.end - args.start + 1,
        "found_chapters": len(found_numbers),
        "source_pages": len(grouped),
        "missing_chapters": len(missing),
        "failed_sources": len(errors),
    }
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(manifest_path, manifest)

    build_combined_backup(
        chapter_dir,
        args.output / "backup.txt",
        args.start,
        args.end,
    )

    saved = sum(
        1
        for number in range(args.start, args.end + 1)
        if (chapter_dir / f"{number}.txt").exists()
    )
    print(f"Готово. Сохранено файлов: {saved}")
    print(f"Нет ссылок: {len(manifest['missing'])}")
    print(f"Ошибок: {len(manifest['errors'])}")
    return 1 if errors or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
