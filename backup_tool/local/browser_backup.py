from __future__ import annotations

import argparse
import json
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
    "пользователь не авторизован.",
    "необходимо авторизоваться",
    "необходимо войти",
    "войдите в аккаунт",
    "войдите в учетную запись",
    "авторизуйтесь",
    "для чтения войдите",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download iFreedom chapters through a real browser session. "
            "Use only for content your account is legitimately allowed to access."
        )
    )
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--end", type=int, default=DEFAULT_END)
    parser.add_argument("--output", type=Path, default=Path("backup"))
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--book-url", default=BOOK_URL)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(".browser-profile"),
        help="Persistent Chromium profile. Cookies/session stay here and are gitignored.",
    )
    return parser.parse_args()


def contains_auth_warning(text: str) -> bool:
    low = " ".join(text.split()).lower()
    return any(marker in low for marker in AUTH_WARNINGS)


def looks_suspicious(text: str) -> bool:
    normalized = " ".join(text.split()).strip()
    if len(normalized) < 300:
        return True
    return contains_auth_warning(normalized)


def fetch_with_browser(page, url: str, *, timeout_ms: int, retries: int, delay: float):
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(700)
            if response is None:
                raise RuntimeError("browser navigation returned no response")
            status = response.status
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
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
            "iFreedom returned an authorization warning. "
            "Log in with an account that has access to this chapter, then retry."
        )

    title, text = extract_text(html)
    if looks_suspicious(text):
        raise ValueError(
            f"extracted text looks incomplete ({len(text)} chars); "
            "the chapter was not saved"
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

    profile = args.profile.resolve()
    profile.mkdir(parents=True, exist_ok=True)

    print(f"Источник: {args.book_url}")
    print(f"Диапазон: {args.start}-{args.end}")
    print("Собираю ссылки...")

    # Link discovery stays public; the actual chapter requests are performed
    # in the logged-in browser session.
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

    print()
    print("Откроется Chromium с отдельным профилем.")
    print("1) Войди в iFreedom в открывшемся окне.")
    print("2) Открой страницу книги и убедись, что главы доступны.")
    print("3) Вернись в консоль и нажми Enter.")
    print()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(
                normalize_url(args.book_url, args.book_url),
                wait_until="domcontentloaded",
                timeout=args.timeout * 1000,
            )
            page.wait_for_timeout(700)
            print(f"Текущая страница: {page.url}")
            input("После успешного входа и проверки доступа нажми Enter... ")

            # A visible browser gives the user a chance to finish login or
            # handle a normal site prompt. No CAPTCHA/auth bypass is attempted.
            book_text = page.locator("body").inner_text(timeout=args.timeout * 1000)
            if contains_auth_warning(book_text):
                print(
                    "[WARN] Браузер всё ещё выглядит неавторизованным. "
                    "Продолжение остановлено, чтобы не сохранить неполные главы."
                )
                return 2

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
                    errors.append({
                        "number": number,
                        "url": url,
                        "error": str(exc),
                    })
                    print(f"[ERR ] {number}: {exc}", file=sys.stderr)

            manifest["missing"] = sorted(set(missing))
            manifest["errors"] = errors
            manifest["range"] = {"start": args.start, "end": args.end}
            manifest["source"] = args.book_url
            manifest["mode"] = "authenticated_browser"
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
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
