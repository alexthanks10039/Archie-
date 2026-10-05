from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
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
    normalize_url,
)

CHROME_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authorize Archie through the user's normal Chrome profile, then run the canonical CLI downloader."
    )
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--end", type=int, default=DEFAULT_END)
    parser.add_argument("--output", type=Path, default=Path("backup"))
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--book-url", default=BOOK_URL)
    parser.add_argument("--wait-browser", type=int, default=180)
    return parser.parse_args()

def find_chrome() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate.resolve()
    found = shutil.which("chrome.exe")
    if found:
        return Path(found).resolve()
    raise FileNotFoundError("Не найден Google Chrome.")

def default_chrome_user_data() -> Path:
    return (Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data").resolve()

def read_cdp_endpoint() -> str | None:
    path = default_chrome_user_data() / "DevToolsActivePort"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if len(lines) < 2:
        return None
    port, ws_path = lines[0].strip(), lines[1].strip()
    if not port.isdigit() or not ws_path.startswith("/devtools/browser/"):
        return None
    return f"ws://127.0.0.1:{port}{ws_path}"

def launch_normal_chrome(chrome: Path, book_url: str) -> subprocess.Popen:
    return subprocess.Popen(
        [str(chrome), normalize_url(book_url, book_url), "chrome://inspect/#remote-debugging"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

def wait_for_cdp(wait_seconds: int) -> str:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        endpoint = read_cdp_endpoint()
        if endpoint:
            return endpoint
        time.sleep(0.5)
    raise TimeoutError(
        "Chrome не предоставил CDP endpoint. В chrome://inspect/#remote-debugging "
        "включи Allow remote debugging for this browser instance."
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

def is_auth_warning(text: str) -> bool:
    value = " ".join(text.split()).lower()
    return any(marker in value for marker in AUTH_WARNINGS)

def main() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cookie_path = args.output / ".archie-session-cookies.json"
    chrome_process = None

    try:
        chrome = find_chrome()
        print(f"Chrome: {chrome}")
        print("Открываю обычный Chrome. Отдельный профиль Archie не используется.")
        chrome_process = launch_normal_chrome(chrome, args.book_url)
        print("Войди в iFreedom через VK обычным способом.")
        input("После успешного входа нажми Enter здесь... ")

        with sync_playwright() as p:
            endpoint = wait_for_cdp(args.wait_browser)
            browser = p.chromium.connect_over_cdp(endpoint, timeout=30_000)
            if not browser.contexts:
                raise RuntimeError("Не найдена Chrome browser context.")
            context = browser.contexts[0]
            pages = context.pages
            page = pages[0] if pages else context.new_page()

            try:
                body = page.locator("body").inner_text(timeout=args.timeout * 1000)
            except PlaywrightTimeoutError:
                body = ""

            if is_auth_warning(body):
                raise PermissionError(
                    "В Chrome iFreedom всё ещё показывает страницу без авторизации."
                )

            cookies = context.cookies([args.book_url])
            if not cookies:
                raise PermissionError(
                    "Не удалось получить cookies авторизованной сессии iFreedom из Chrome."
                )

            cookie_path.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Получено cookies: {len(cookies)}")
            print("Авторизация передана каноническому CLI-загрузчику.")

            cmd = [
                sys.executable,
                "-u",
                str(Path(__file__).with_name("backup.py")),
                "--start", str(args.start),
                "--end", str(args.end),
                "--output", str(args.output),
                "--delay", str(args.delay),
                "--retries", str(args.retries),
                "--timeout", str(args.timeout),
                "--max-pages", str(args.max_pages),
                "--book-url", args.book_url,
                "--cookies-file", str(cookie_path),
            ]
            result = subprocess.run(cmd, cwd=str(Path(__file__).parent), check=False)
            return result.returncode

    except KeyboardInterrupt:
        print("Остановлено пользователем.")
        return 130
    except Exception as exc:
        print(f"Критическая ошибка авторизации: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            cookie_path.unlink(missing_ok=True)
        except OSError:
            pass
        if chrome_process is not None:
            print(f"Chrome PID {chrome_process.pid} оставлен работать в обычной сессии.")

if __name__ == "__main__":
    raise SystemExit(main())
