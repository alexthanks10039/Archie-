from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
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
    group_chapter_links,
    load_manifest,
    normalize_url,
    save_manifest,
    save_merged_chapters,
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


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


class RunLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a", encoding="utf-8", buffering=1)

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"[{now_text()}] [{level}] {message}"
        print(line, flush=True)
        try:
            self.file.write(line + "\n")
            self.file.flush()
        except OSError:
            pass

    def exception(self, message: str, exc: BaseException) -> None:
        self.log(f"{message}: {type(exc).__name__}: {exc}", "ERROR")
        trace = traceback.format_exc()
        try:
            self.file.write(trace.rstrip() + "\n")
            self.file.flush()
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.file.close()
        except OSError:
            pass


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
        "Не найден Google Chrome. Установи Chrome или проверь стандартный путь установки."
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


def launch_normal_chrome(chrome: Path, book_url: str, browser_log_path: Path, logger: RunLogger) -> subprocess.Popen:
    inspect_url = "chrome://inspect/#remote-debugging"
    try:
        browser_log = browser_log_path.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        logger.exception("Не удалось открыть browser.log", exc)
        browser_log = subprocess.DEVNULL

    logger.log(f"Запускаю Chrome: {chrome}")
    logger.log(f"Chrome user-data-dir: {default_chrome_user_data()}")
    logger.log("Изолированный --user-data-dir НЕ используется.")

    try:
        process = subprocess.Popen(
            [str(chrome), normalize_url(book_url, book_url), inspect_url],
            stdout=browser_log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception:
        if browser_log not in (subprocess.DEVNULL, None):
            browser_log.close()
        raise

    logger.log(f"Chrome process PID: {process.pid}")
    return process


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


def wait_for_cdp(wait_seconds: int, logger: RunLogger) -> str:
    deadline = time.monotonic() + wait_seconds
    last_port_file = devtools_active_port_file()
    logger.log(f"Жду Chrome CDP до {wait_seconds} сек.")
    logger.log(f"Проверяю файл: {last_port_file}")

    while time.monotonic() < deadline:
        endpoint = read_cdp_endpoint()
        if endpoint:
            logger.log("Chrome CDP endpoint найден.")
            return endpoint
        time.sleep(0.5)

    raise TimeoutError(
        "Chrome не предоставил CDP endpoint. "
        "Открой chrome://inspect/#remote-debugging и включи "
        "«Allow remote debugging for this browser instance»."
    )


def contains_auth_warning(text: str) -> bool:
    low = " ".join(text.split()).lower()
    return any(marker in low for marker in AUTH_WARNINGS)


def looks_suspicious(text: str) -> bool:
    normalized = " ".join(text.split()).strip()
    return len(normalized) < 300 or contains_auth_warning(normalized)


def fetch_with_browser(page, url: str, *, timeout_ms: int, retries: int, delay: float, logger: RunLogger):
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            logger.log(f"Открываю URL, попытка {attempt}/{retries}: {url}")
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1200)
            if response is None:
                raise RuntimeError("браузер не вернул HTTP response")
            logger.log(f"HTTP {response.status}; фактический URL: {page.url}")
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            if delay:
                time.sleep(delay)
            return response
        except (PlaywrightTimeoutError, RuntimeError) as exc:
            last_error = exc
            logger.log(
                f"Ошибка открытия страницы, попытка {attempt}/{retries}: "
                f"{type(exc).__name__}: {exc}",
                "WARN",
            )
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
    logger: RunLogger,
) -> dict:
    response = fetch_with_browser(
        page,
        url,
        timeout_ms=timeout_ms,
        retries=retries,
        delay=delay,
        logger=logger,
    )
    html = page.content()

    if contains_auth_warning(html):
        raise PermissionError(
            "iFreedom сообщает, что пользователь не авторизован. Глава не сохранена."
        )

    title, text = extract_text(html)
    logger.log(f"Глава {number}: извлечено {len(text)} символов; title={title!r}")

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


def save_fatal_manifest(
    manifest_path: Path,
    manifest: dict,
    *,
    stage: str,
    exc: BaseException,
) -> None:
    manifest.setdefault("errors", [])
    manifest["fatal_error"] = {
        "stage": stage,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        save_manifest(manifest_path, manifest)
    except Exception:
        pass


def main() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    log_path = args.output / "archie.log"
    browser_log_path = args.output / "browser.log"

    logger = RunLogger(log_path)
    manifest = load_manifest(manifest_path)
    manifest.setdefault("chapters", {})
    manifest.setdefault("missing", [])
    manifest.setdefault("errors", [])
    manifest["last_run"] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "cwd": str(Path.cwd()),
        "output": str(args.output.resolve()),
        "start": args.start,
        "end": args.end,
    }
    save_manifest(manifest_path, manifest)

    browser_process = None
    browser_log_file = None
    browser = None

    try:
        if args.start < 1 or args.end < args.start:
            raise ValueError("--start must be <= --end")

        chapter_dir = args.output / "chapters"
        chapter_dir.mkdir(parents=True, exist_ok=True)

        logger.log("=" * 70)
        logger.log(f"Archie run: chapters {args.start}-{args.end}")
        logger.log(f"Python: {sys.version.replace(chr(10), ' ')}")
        logger.log(f"CWD: {Path.cwd()}")
        logger.log(f"Output: {args.output.resolve()}")
        logger.log(f"Manifest: {manifest_path.resolve()}")
        logger.log(f"Log: {log_path.resolve()}")
        logger.log(f"Browser log: {browser_log_path.resolve()}")

        logger.log("Собираю ссылки публичным HTTP-клиентом.")
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

        try:
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
        except Exception as exc:
            logger.exception("Не удалось собрать ссылки на главы", exc)
            raise

        grouped = group_chapter_links(links)
        logger.log(f"Найдено глав: {len(links)}")
        logger.log(f"Найдено страниц-источников: {len(grouped)}")

        missing = [
            number
            for number in range(args.start, args.end + 1)
            if number not in links
        ]
        for number in missing:
            logger.log(f"[MISS] {number}: ссылка не найдена", "WARN")

        manifest["missing"] = sorted(set(missing))
        manifest["stats"] = {
            "expected_chapters": args.end - args.start + 1,
            "found_chapters": len(links),
            "source_pages": len(grouped),
        }
        manifest["range"] = {"start": args.start, "end": args.end}
        manifest["source"] = args.book_url
        save_manifest(manifest_path, manifest)

        chrome = find_chrome()
        logger.log(f"Chrome найден: {chrome}")
        browser_process = launch_normal_chrome(
            chrome,
            args.book_url,
            browser_log_path,
            logger,
        )

        logger.log("Теперь войди в iFreedom через VK в обычном Chrome.")
        logger.log("После входа нажми кнопку «Я вошёл в iFreedom» в GUI.")

        input("Готово с авторизацией? Нажми Enter... ")

        with sync_playwright() as p:
            endpoint = wait_for_cdp(args.wait_browser, logger)
            logger.log("Подключаюсь к Chrome через CDP.")
            browser = p.chromium.connect_over_cdp(endpoint, timeout=30_000)
            logger.log(f"Chrome contexts: {len(browser.contexts)}")

            if not browser.contexts:
                raise RuntimeError("Не найдена активная Chrome browser context.")

            context = browser.contexts[0]
            pages = context.pages
            logger.log(f"Открытых вкладок: {len(pages)}")
            page = pages[0] if pages else context.new_page()

            try:
                body_text = page.locator("body").inner_text(timeout=args.timeout * 1000)
                if contains_auth_warning(body_text):
                    raise PermissionError(
                        "В открытой Chrome-сессии iFreedom всё ещё показывает "
                        "сообщение об отсутствии авторизации."
                    )
            except PlaywrightTimeoutError as exc:
                logger.log(
                    "Не удалось прочитать body открытой вкладки для проверки авторизации.",
                    "WARN",
                )
                logger.exception("Playwright timeout during auth check", exc)

            errors = []
            source_items = list(grouped.items())
            for source_index, (url, numbers) in enumerate(source_items, start=1):
                relevant = [
                    number
                    for number in numbers
                    if args.start <= number <= args.end
                ]
                if not relevant:
                    continue

                try:
                    logger.log(
                        f"[SOURCE] {source_index}/{len(source_items)} "
                        f"chapters={relevant}: {url}"
                    )

                    if len(relevant) == 1:
                        number = relevant[0]
                        target = chapter_dir / f"{number}.txt"
                        record = save_browser_chapter(
                            page,
                            number,
                            url,
                            chapter_dir,
                            timeout_ms=args.timeout * 1000,
                            retries=args.retries,
                            delay=args.delay,
                            logger=logger,
                        )
                        manifest["chapters"][str(number)] = record
                        logger.log(
                            f"[OK] {number}: {record['chars']} chars, {record['bytes']} bytes"
                        )
                    else:
                        logger.log(
                            f"[MERGED] source page: {url}; "
                            f"detected chapters: {', '.join(map(str, relevant))}"
                        )
                        response = fetch_with_browser(
                            page,
                            url,
                            timeout_ms=args.timeout * 1000,
                            retries=args.retries,
                            delay=args.delay,
                            logger=logger,
                        )
                        html = page.content()
                        if contains_auth_warning(html):
                            raise PermissionError(
                                "iFreedom сообщает, что пользователь не авторизован."
                            )

                        records = save_merged_chapters(
                            html,
                            page.url,
                            relevant,
                            chapter_dir,
                        )
                        for number, record in records.items():
                            manifest["chapters"][str(number)] = record
                            logger.log(
                                f"[MERGED] chapter {number}: "
                                f"{record['chars']} chars, {record['bytes']} bytes"
                            )

                    save_manifest(manifest_path, manifest)

                except (
                    PlaywrightTimeoutError,
                    PermissionError,
                    RuntimeError,
                    ValueError,
                    OSError,
                ) as exc:
                    error_record = {
                        "url": url,
                        "chapter_numbers": relevant,
                        "type": type(exc).__name__,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                    errors.append(error_record)
                    manifest["errors"] = errors
                    save_manifest(manifest_path, manifest)
                    logger.exception(
                        f"[ERR] Source page for chapters {relevant}",
                        exc,
                    )

            manifest["missing"] = sorted(set(missing))
            manifest["errors"] = errors
            manifest["range"] = {"start": args.start, "end": args.end}
            manifest["source"] = args.book_url
            manifest["mode"] = "existing_chrome_profile_cdp"
            manifest["last_run"]["finished_at"] = datetime.now(timezone.utc).isoformat()
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

            logger.log(f"Готово. Сохранено файлов: {saved}")
            logger.log(f"Нет ссылок: {len(manifest['missing'])}")
            logger.log(f"Ошибок: {len(manifest['errors'])}")

            if errors or missing:
                return 1
            return 0

    except Exception as exc:
        stage = "startup_or_runtime"
        logger.exception(f"Критическая ошибка: {stage}", exc)
        save_fatal_manifest(manifest_path, manifest, stage=stage, exc=exc)
        try:
            manifest["last_run"]["finished_at"] = datetime.now(timezone.utc).isoformat()
            save_manifest(manifest_path, manifest)
        except Exception:
            pass
        return 2

    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:
                logger.exception("Ошибка при закрытии Playwright browser connection", exc)
        if browser_process is not None:
            logger.log(f"Chrome PID {browser_process.pid} завершает работу: {browser_process.poll()}")
            # Do not kill the user's normal Chrome session.
        logger.log(f"Логи сохранены: {log_path.resolve()}")
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
