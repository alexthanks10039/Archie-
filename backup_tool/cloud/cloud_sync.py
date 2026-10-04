from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests

DROPBOX_API = "https://api.dropboxapi.com"
DROPBOX_CONTENT = "https://content.dropboxapi.com"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
CHUNK_SIZE = 8 * 1024 * 1024


def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


# ---------- Dropbox ----------

def dropbox_token() -> str:
    r = requests.post(
        f"{DROPBOX_API}/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": env("DROPBOX_REFRESH_TOKEN"),
            "client_id": env("DROPBOX_APP_KEY"),
            "client_secret": env("DROPBOX_APP_SECRET"),
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def dropbox_download(remote: str, local: Path) -> bool:
    token = dropbox_token()
    r = requests.post(
        f"{DROPBOX_CONTENT}/2/files/download",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({"path": remote}),
        },
        timeout=600,
    )
    if r.status_code == 409 and "path/not_found" in r.text:
        return False
    r.raise_for_status()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(r.content)
    return True


def dropbox_upload(local: Path, remote: str) -> None:
    token = dropbox_token()
    size = local.stat().st_size

    with local.open("rb") as fh:
        first = fh.read(min(CHUNK_SIZE, size))

        if size <= CHUNK_SIZE:
            r = requests.post(
                f"{DROPBOX_CONTENT}/2/files/upload",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": json.dumps({
                        "path": remote,
                        "mode": "overwrite",
                        "autorename": False,
                        "mute": True,
                    }),
                },
                data=first,
                timeout=600,
            )
            r.raise_for_status()
            return

        r = requests.post(
            f"{DROPBOX_CONTENT}/2/files/upload_session/start",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": json.dumps({"close": False}),
            },
            data=first,
            timeout=600,
        )
        r.raise_for_status()
        session_id = r.json()["session_id"]
        offset = len(first)

        while offset < size:
            chunk = fh.read(min(CHUNK_SIZE, size - offset))
            is_last = offset + len(chunk) >= size

            if is_last:
                r = requests.post(
                    f"{DROPBOX_CONTENT}/2/files/upload_session/finish",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": json.dumps({
                            "cursor": {
                                "session_id": session_id,
                                "offset": offset,
                            },
                            "commit": {
                                "path": remote,
                                "mode": "overwrite",
                                "autorename": False,
                                "mute": True,
                            },
                        }),
                    },
                    data=chunk,
                    timeout=600,
                )
            else:
                r = requests.post(
                    f"{DROPBOX_CONTENT}/2/files/upload_session/append_v2",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": json.dumps({
                            "cursor": {
                                "session_id": session_id,
                                "offset": offset,
                            },
                            "close": False,
                        }),
                    },
                    data=chunk,
                    timeout=600,
                )

            r.raise_for_status()
            offset += len(chunk)


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Archie archives with Dropbox.")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload")
    up.add_argument("--local", type=Path, required=True)
    up.add_argument("--name", default=None)
    up.add_argument("--dropbox", default=None)

    down = sub.add_parser("download")
    down.add_argument("--output", type=Path, required=True)
    down.add_argument("--dropbox", default=None)

    args = parser.parse_args()

    if args.command == "download":
        if args.dropbox and dropbox_download(args.dropbox, args.output):
            print(f"Restored from Dropbox: {args.output}")
            return 0



        print("No previous cloud archive found; starting fresh.")
        return 0

    if args.dropbox:
        dropbox_upload(args.local, args.dropbox)
        print(f"Uploaded to Dropbox: {args.dropbox}")


    return 0


if __name__ == "__main__":
    raise SystemExit(main())
