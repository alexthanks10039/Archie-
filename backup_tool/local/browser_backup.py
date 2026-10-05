from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from backup import (
    BOOK_URL,
    DEFAULT_DELAY,
    DEFAULT_END,
    DEFAULT_RETRIES,
    DEFAULT_START,
    DEFAULT_TIMEOUT,
    build_combined_backup,
    collect_chapter_links,
    extract_text,
    load_manifest,
    normalize_url,
    save_manifest,
    sha256,
)

AUTH_WARNINGS = (
    "пользователь не авторизован",
    "необходимо авторизоваться",
    "необходимо войти",
    "войдите в аккаунт",
    "войдите в учетную запись",
    "авторизуйтесь",
    "для чтения войдите",
)

CHROME_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download iFreedom chapters through the user's normal Chrome profile."
    )
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--end", type=int, default=DEFAULT_END)
    parser.add_argument("--output", type=Path, default=Path("backup"))
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--book-url", default=BOOK_URL)
    parser.add_argument(
        "--wait-browser",
        type=int,
        default=180,
        help="Seconds to wait for Chrome remote debugging after the user enables it.",
    )
    return parser.parse_args()


def find_chrome() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate.resolve()
    found = shutil.which("chrome.exe")
    if found:
        return Path(found).resolve()
    raise FileNotFoundError(
        "Не найден Google Chrome. Установи Chrome или укажи путь к chrome.exe."
    )


def default_chrome_user_data() -> Path:
    return (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google"
        / "Chrome"
        / "User Data"
    ).resolve()


def devtools_active_port_file() -> Path:
    return default_chrome_user_data() / "DevToolsActivePort"


def launch_normal_chrome(chrome: Path, book_url: str) -> None:
    # Deliberately do NOT use --user-data-dir or any isolated profile.
    # Chrome opens using the user's ordinary profile and existing cookies.
    inspect_url = "chrome://inspect/#remote-debugging"
    subprocess.Popen(
        [str(chrome), normalize_url(book_url, book_url), inspect_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def read_cdp_endpoint() -> str | None:
    path = devtools_active_port_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if len(lines) < 2:
        return None
    port = lines[0].strip()
    ws_path = lines[1].strip()
    if not port.isdigit() or not ws_path.startswith("/devtools/browser/"):
        return None
    return f"ws://127.0.0.1:{port}{ws_path}"


def wait_for_cdp(wait_seconds: int) -> str:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        endpoint = read_cdp_endpoint()
        if endpoint:
            return endpoint
        time.sleep(0.5)
    raise TimeoutError(
        "Chrome не предоставил CDP endpoint. Открой chrome://inspect/#remote-debugging "
        "и включи «Allow remote debugging for this browser instance»."
    )


def attach_to_chrome(playwright):
    endpoint = wait_for_cdp(180)
    browser = playwright.chromium.connect_over_cdp(endpoint, timeout=30_000)
    if not browser.contexts:
        raise RuntimeError("Chrome подключился, но активная browser context не найдена.")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    return browser, context, page


def contains_auth_warning(text: str) -> bool:
    low = " ".join(text.split()).lower()
    return any(marker in low for marker in AUTH_WARNINGS)


def looks_suspicious(text: str) -> bool:
    normalized = " ".join(text.split()).strip()
    return len(normalized) < 300 or contains_auth_warning(normalized)


def fetch_with_browser(page, url: str, *, timeout_ms: int, retries: int, delay: float):
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1200)
            if response is None:
                raise RuntimeError("браузер не вернул ответ")
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            if delay:
                time.sleep(delay)
            return response
        except (PlaywrightTimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2.0 * attempt, 5.0))
    assert last_error is not None
    raise last_error


def save_browser_chapter(
    page,
    number: int,
    url: str,
    chapter_dir: Path,
    *,
    timeout_ms: int,
    retries: int,
    delay: float,
) -> dict:
    response = fetch_with_browser(
        page,
        url,
        timeout_ms=timeout_ms,
        retries=retries,
        delay=delay,
    )
    html = page.content()
    if contains_auth_warning(html):
        raise PermissionError(
            "iFreedom сообщает, что пользователь не авторизован. Глава не сохранена."
        )

    title, text = extract_text(html)
    if looks_suspicious(text):
        raise ValueError(
            f"текст выглядит неполным ({len(text)} символов). Глава не сохранена."
        )

    output = chapter_dir / f"{number}.txt"
    output.write_text(f"{title}\n\n{text}\n", encoding="utf-8")

    return {
        "number": number,
        "url": url,
        "final_url": page.url,
        "status": response.status,
        "title": title,
        "chars": len(text),
        "bytes": output.stat().st_size,
        "sha256": sha256(output),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "file": str(output.as_posix()),
    }


def main() -> int:
    args = parse_args()
    if args.start < 1 or args.end < args.start:
        print("--start must be <= --end", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    chapter_dir = args.output / "chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    manifest = load_manifest(manifest_path)
    manifest.setdefault("chapters", {})
    manifest.setdefault("missing", [])
    manifest.setdefault("errors", [])

    print(f"Источник: {args.book_url}")
    print(f"Диапазон: {args.start}-{args.end}")
    print("Собираю ссылки...")

    from requests import Session

    session = Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })

    links = collect_chapter_links(
        session,
        args.book_url,
        args.start,
        args.end,
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        max_pages=100,
    )
    print(f"Найдено ссылок: {len(links)}")

    missing = []
    pending = []
    for number in range(args.start, args.end + 1):
        url = links.get(number)
        if not url:
            missing.append(number)
            print(f"[MISS] {number}: ссылка на главу не найдена")
        else:
            pending.append((number, url))

    chrome = find_chrome()
    print()
    print("Открываю обычный Google Chrome без отдельного профиля.")
    print("В Chrome откроются книга iFreedom и настройка Remote Debugging.")
    print("Включи «Allow remote debugging for this browser instance».")
    print("Затем войди в iFreedom через VK и проверь полный текст главы.")
    print("После этого вернись в Archie и нажми Enter.")
    launch_normal_chrome(chrome, args.book_url)

    browser = None
    try:
        input("Готово с авторизацией? Нажми Enter... ")
        with sync_playwright() as p:
            endpoint = wait_for_cdp(args.wait_browser)
            print(f"Chrome CDP найден: {endpoint.split('/devtools/')[0]}")
            browser = p.chromium.connect_over_cdp(endpoint, timeout=30_000)
            if not browser.contexts:
                raise RuntimeError("Не найдена активная Chrome session.")
            context = browser.contexts[0]
            pages = context.pages
            page = pages[0] if pages else context.new_page()

            errors = []
            for index, (number, url) in enumerate(pending, start=1):
                target = chapter_dir / f"{number}.txt"
                try:
                    print(f"[GET ] {number} ({index}/{len(pending)}) {url}")
                    record = save_browser_chapter(
                        page,
                        number,
                        url,
                        chapter_dir,
                        timeout_ms=args.timeout * 1000,
                        retries=args.retries,
                        delay=args.delay,
                    )
                    manifest["chapters"][str(number)] = record
                    save_manifest(manifest_path, manifest)
                    print(f"[OK  ] {number}: {record['chars']} chars")
                except (
                    PlaywrightTimeoutError,
                    PermissionError,
                    RuntimeError,
                    ValueError,
                    OSError,
                ) as exc:
                    if target.exists():
                        target.unlink()
                    errors.append({"number": number, "url": url, "error": str(exc)})
                    print(f"[ERR ] {number}: {exc}", file=sys.stderr)

            manifest["missing"] = sorted(set(missing))
            manifest["errors"] = errors
            manifest["range"] = {"start": args.start, "end": args.end}
            manifest["source"] = args.book_url
            manifest["mode"] = "existing_chrome_profile_cdp"
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
            print()
            print(f"Готово. Сохранено файлов: {saved}")
            print(f"Нет ссылок: {len(manifest['missing'])}")
            print(f"Ошибок: {len(manifest['errors'])}")
            return 0 if not errors and not missing else 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
