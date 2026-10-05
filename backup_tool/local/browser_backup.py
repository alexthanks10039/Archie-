from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
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

BROWSER_CANDIDATES = {
    "yandex": (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Yandex/YandexBrowser/Application/browser.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Yandex/YandexBrowser/Application/browser.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Yandex/YandexBrowser/Application/browser.exe",
    ),
    "chrome": (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
    ),
    "edge": (
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download iFreedom chapters through a user-authenticated Chromium browser."
    )
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--end", type=int, default=DEFAULT_END)
    parser.add_argument("--output", type=Path, default=Path("backup"))
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--book-url", default=BOOK_URL)
    parser.add_argument("--browser", choices=("yandex", "chrome", "edge"), default="yandex")
    parser.add_argument("--browser-path", type=Path, default=None)
    parser.add_argument("--debug-port", type=int, default=0)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(".browser-profile"),
        help="Dedicated browser profile. It stores the login session locally.",
    )
    return parser.parse_args()


def contains_auth_warning(text: str) -> bool:
    low = " ".join(text.split()).lower()
    return any(marker in low for marker in AUTH_WARNINGS)


def looks_suspicious(text: str) -> bool:
    normalized = " ".join(text.split()).strip()
    return len(normalized) < 300 or contains_auth_warning(normalized)


def find_browser(browser: str, explicit: Path | None) -> Path:
    if explicit:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Браузер не найден: {path}")
        return path

    for candidate in BROWSER_CANDIDATES[browser]:
        if candidate and candidate.is_file():
            return candidate.resolve()

    aliases = {
        "yandex": ("browser.exe",),
        "chrome": ("chrome.exe",),
        "edge": ("msedge.exe",),
    }
    for name in aliases[browser]:
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved).resolve()

    raise FileNotFoundError(
        f"Не найден {browser}. Укажи путь к browser.exe через --browser-path."
    )


def find_free_port(start: int = 9222) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("Не удалось найти свободный порт для браузера.")


def wait_for_cdp(port: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    endpoint = f"http://127.0.0.1:{port}/json/version"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=1.5) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(
        f"Не дождался запуска браузера на порту {port}: {last_error}"
    )


def launch_real_browser(
    executable: Path,
    profile: Path,
    port: int,
    url: str,
) -> subprocess.Popen:
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def attach_to_browser(playwright, port: int):
    browser = playwright.chromium.connect_over_cdp(
        f"http://127.0.0.1:{port}",
        timeout=30_000,
        is_local=True,
        no_defaults=True,
    )
    contexts = browser.contexts
    if not contexts:
        raise RuntimeError("Браузер запущен, но Playwright не увидел контекст.")
    context = contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    return browser, context, page


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
            "iFreedom сообщает, что пользователь не авторизован. "
            "Глава не сохранена."
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

    executable = find_browser(args.browser, args.browser_path)
    port = args.debug_port or find_free_port()
    profile = args.profile.resolve()

    print()
    print(f"Браузер: {executable}")
    print(f"Профиль: {profile}")
    print(f"CDP порт: {port}")
    print()
    print("Запускаю обычный видимый браузер.")
    print("Войди в iFreedom через VK вручную.")
    print("После входа открой книгу и проверь, что глава читается полностью.")
    print("Затем нажми Enter в Archie.")

    browser_process = launch_real_browser(
        executable,
        profile,
        port,
        normalize_url(args.book_url, args.book_url),
    )

    browser = None
    try:
        wait_for_cdp(port, args.timeout)
        with sync_playwright() as p:
            browser, context, page = attach_to_browser(p, port)
            input("После успешного входа и проверки доступа нажми Enter... ")

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
            manifest["mode"] = f"real_{args.browser}_browser_cdp"
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
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            browser_process.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
