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


# ---------- Google Drive ----------

def google_token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(env("GOOGLE_SERVICE_ACCOUNT_JSON")),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    credentials.refresh(Request())
    return credentials.token


def google_find(name: str, folder_id: str, token: str) -> str | None:
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and "
        f"'{folder_id}' in parents and trashed = false"
    )
    r = requests.get(
        f"{DRIVE_API}/files",
        params={
            "q": query,
            "fields": "files(id,name)",
            "pageSize": 10,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def google_download(remote_name: str, folder_id: str, local: Path) -> bool:
    token = google_token()
    file_id = google_find(remote_name, folder_id, token)
    if not file_id:
        return False

    r = requests.get(
        f"{DRIVE_API}/files/{file_id}",
        params={"alt": "media"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=900,
    )
    r.raise_for_status()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(r.content)
    return True


def google_upload(local: Path, folder_id: str, name: str | None = None) -> None:
    token = google_token()
    name = name or local.name
    existing = google_find(name, folder_id, token)

    if existing:
        url = f"{DRIVE_UPLOAD}/{existing}"
        method = "PATCH"
        metadata = None
    else:
        url = DRIVE_UPLOAD
        method = "POST"
        metadata = {
            "name": name,
            "parents": [folder_id],
            "mimeType": "application/octet-stream",
        }

    total = local.stat().st_size
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Upload-Content-Length": str(total),
        "X-Upload-Content-Type": "application/octet-stream",
    }
    if existing:
        params = {"uploadType": "resumable"}
    else:
        params = {"uploadType": "resumable"}

    if metadata is not None:
        headers["Content-Type"] = "application/json; charset=UTF-8"
        r = requests.post(
            url,
            params=params,
            headers=headers,
            data=json.dumps(metadata),
            timeout=60,
        )
    else:
        r = requests.patch(
            url,
            params=params,
            headers=headers,
            timeout=60,
        )

    r.raise_for_status()
    upload_url = r.headers.get("Location")
    if not upload_url:
        raise RuntimeError("Google Drive did not return a resumable upload URL")

    with local.open("rb") as fh:
        offset = 0
        while offset < total:
            chunk = fh.read(min(CHUNK_SIZE, total - offset))
            end = offset + len(chunk) - 1
            r = requests.put(
                upload_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                    "Content-Type": "application/octet-stream",
                },
                data=chunk,
                timeout=900,
            )

            if r.status_code in (200, 201):
                break

            if r.status_code != 308:
                r.raise_for_status()

            offset += len(chunk)


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Archie archives with Google Drive and Dropbox.")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload")
    up.add_argument("--local", type=Path, required=True)
    up.add_argument("--name", default=None)
    up.add_argument("--dropbox", default=None)
    up.add_argument("--google-folder", default=None)

    down = sub.add_parser("download")
    down.add_argument("--output", type=Path, required=True)
    down.add_argument("--dropbox", default=None)
    down.add_argument("--google-folder", default=None)
    down.add_argument("--google-name", default="archie-backup.zip")

    args = parser.parse_args()

    if args.command == "download":
        if args.dropbox and dropbox_download(args.dropbox, args.output):
            print(f"Restored from Dropbox: {args.output}")
            return 0

        if args.google_folder and google_download(
            args.google_name, args.google_folder, args.output
        ):
            print(f"Restored from Google Drive: {args.output}")
            return 0

        print("No previous cloud archive found; starting fresh.")
        return 0

    if args.dropbox:
        dropbox_upload(args.local, args.dropbox)
        print(f"Uploaded to Dropbox: {args.dropbox}")

    if args.google_folder:
        google_upload(args.local, args.google_folder, args.name)
        print(f"Uploaded to Google Drive folder: {args.google_folder}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
