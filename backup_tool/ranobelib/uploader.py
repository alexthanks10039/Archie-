from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from ranobelib_loader import RanobelibLoader
except ImportError:
    RanobelibLoader = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload numbered Archie chapter text files to RanobeLib.")
    parser.add_argument("--input", type=Path, default=Path("../local/backup/chapters"), help="Folder containing N.txt chapter files.")
    parser.add_argument("--start", type=int, required=True, help="First chapter number.")
    parser.add_argument("--end", type=int, required=True, help="Last chapter number.")
    parser.add_argument("--manga-id", type=int, required=True, help="RanobeLib title ID.")
    parser.add_argument("--branch-id", type=int, required=True, help="Translation branch ID.")
    parser.add_argument("--teams", default="", help="Comma-separated team IDs, e.g. 123,456.")
    parser.add_argument("--volume", type=int, default=1, help="Volume number (default: 1).")
    parser.add_argument("--name-template", default="Глава {number}", help="Template fields: number, source_title.")
    parser.add_argument("--use-source-title", action="store_true", help="Use the first non-empty line as chapter title.")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between uploads (default: 2).")
    parser.add_argument("--state", type=Path, default=Path("upload_state.json"), help="Resume state file.")
    parser.add_argument("--publish", action="store_true", help="Actually upload chapters. Without this flag, preview only.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry chapters marked as failed.")
    return parser.parse_args()


def read_chapter(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        raise ValueError("chapter file is empty")
    lines = raw.splitlines()
    source_title = lines[0].strip() if lines else ""
    body = raw
    for index, line in enumerate(lines[1:], start=1):
        if not line.strip():
            body = "\n".join(lines[index + 1:]).strip()
            break
    if not body:
        body = raw
    return source_title, body


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"chapters": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("chapters"), dict):
            raise ValueError("unexpected state format")
        return value
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"State file is invalid: {path}. Move it aside or repair it manually.") from exc


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def parse_team_ids(value: str) -> list[int]:
    if not value.strip():
        return []
    try:
        ids = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise ValueError("--teams must be comma-separated integer IDs") from exc
    if any(team_id <= 0 for team_id in ids):
        raise ValueError("team IDs must be positive integers")
    return ids


def main() -> int:
    args = parse_args()
    if args.start < 1 or args.end < args.start:
        print("Invalid chapter range: require 1 <= --start <= --end.", file=sys.stderr)
        return 2
    if args.volume < 1 or args.delay < 0:
        print("--volume must be positive and --delay cannot be negative.", file=sys.stderr)
        return 2

    try:
        teams = parse_team_ids(args.teams)
        state = load_state(args.state)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    input_dir = args.input.resolve()
    planned: list[tuple[int, Path, str, str]] = []
    problems: list[str] = []
    for number in range(args.start, args.end + 1):
        path = input_dir / f"{number}.txt"
        if not path.is_file():
            problems.append(f"{number}: file not found ({path})")
            continue
        try:
            source_title, body = read_chapter(path)
            if len(body.strip()) < 20:
                raise ValueError("text is suspiciously short")
            if args.use_source_title and source_title:
                name = source_title
            else:
                name = args.name_template.format(number=number, source_title=source_title)
            name = re.sub(r"\s+", " ", name).strip()[:250]
            planned.append((number, path, name, body))
        except (OSError, UnicodeError, ValueError, KeyError) as exc:
            problems.append(f"{number}: {exc}")

    print(f"Input: {input_dir}")
    print(f"Target title ID: {args.manga_id}; branch ID: {args.branch_id}; volume: {args.volume}")
    print(f"Range: {args.start}-{args.end}; valid files: {len(planned)}")
    print(f"Mode: {'UPLOAD' if args.publish else 'DRY RUN (no changes on RanobeLib)'}")
    if problems:
        print(f"Files needing attention: {len(problems)}")
        for problem in problems[:20]:
            print(f"  [WARN] {problem}")
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more")

    chapters_state = state.setdefault("chapters", {})
    pending = []
    for item in planned:
        number = item[0]
        record = chapters_state.get(str(number), {})
        if record.get("status") == "uploaded":
            print(f"[SKIP] {number}: already marked uploaded")
        elif record.get("status") == "failed" and not args.retry_failed:
            print(f"[SKIP] {number}: previous failure (use --retry-failed to retry)")
        else:
            pending.append(item)

    for number, path, name, body in pending:
        print(f"[PLAN] {number}: {name} ({len(body)} chars, {path.name})")

    if not args.publish:
        print("\nDry run complete. Review the title/branch/team IDs, then rerun with --publish.")
        return 0 if not problems else 1

    token = os.environ.get("RANOBELIB_ACCESS_TOKEN", "").strip()
    if not token:
        print("Missing RANOBELIB_ACCESS_TOKEN environment variable.", file=sys.stderr)
        return 2
    if RanobelibLoader is None:
        print("Missing dependency. Run: python -m pip install -r requirements.txt", file=sys.stderr)
        return 2
    if not teams:
        print("No team IDs provided. Supply --teams with the team/translator IDs required by the title.", file=sys.stderr)
        return 2

    loader = RanobelibLoader(access_token=token)
    failures = 0
    for index, (number, path, name, body) in enumerate(pending):
        try:
            response = loader.load_chapter(
                branch_id=args.branch_id,
                text=body,
                manga_id=args.manga_id,
                name=name,
                number=number,
                volume=args.volume,
                teams=teams,
            )
            response.raise_for_status()
            try:
                response_data = response.json()
            except ValueError:
                response_data = {"response_text": response.text[:1000]}
            chapters_state[str(number)] = {
                "status": "uploaded",
                "chapter_number": number,
                "source_file": str(path),
                "name": name,
                "http_status": response.status_code,
                "response": response_data,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(args.state, state)
            print(f"[OK] {number}: uploaded (HTTP {response.status_code})")
        except Exception as exc:
            failures += 1
            chapters_state[str(number)] = {
                "status": "failed",
                "chapter_number": number,
                "source_file": str(path),
                "name": name,
                "error": str(exc),
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(args.state, state)
            print(f"[ERR] {number}: {exc}", file=sys.stderr)
        if index < len(pending) - 1 and args.delay:
            time.sleep(args.delay)

    print(f"\nFinished. Uploaded: {len(pending) - failures}; failures: {failures}.")
    print(f"Resume state: {args.state.resolve()}")
    return 1 if failures or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
