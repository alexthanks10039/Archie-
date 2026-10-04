from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests

DROPBOX_API = "https://api.dropboxapi.com"
DROPBOX_CONTENT = "https://content.dropboxapi.com"
GOOGLE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"


def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


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
        timeout=180,
    )
    if r.status_code == 409 and "path/not_found" in r.text:
        return False
    r.raise_for_status()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(r.content)
    return True


def dropbox_upload(local: Path, remote: str) -> None:
    token = dropbox_token()
    data = local.read_bytes()
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
        data=data,
        timeout=300,
    )
    r.raise_for_status()


def google_credentials():
    from google.oauth2 import service_account

    raw = env("GOOGLE_SERVICE_ACCOUNT_JSON")
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )


def google_access_token() -> str:
    creds = google_credentials()
    creds.refresh(requests.adapters.HTTPAdapter)
    return creds.token


def google_token():
    creds = google_credentials()
    from google.auth.transport.requests import Request
    creds.refresh(Request())
    return creds.token


def google_find(name: str, folder_id: str, token: str) -> str | None:
    q = (
        f"name = '{name.replace(chr(39), chr(92) + chr(39))}' "
        f"and '{folder_id}' in parents and trashed = false"
    )
    r = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        params={
            "q": q,
            "fields": "files(id,name)",
            "pageSize": 10,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def google_upload(local: Path, folder_id: str, name: str | None = None) -> None:
    token = google_token()
    name = name or local.name
    existing = google_find(name, folder_id, token)

    metadata = {
        "name": name,
        "mimeType": "application/octet-stream",
        "parents": [folder_id],
    }

    if existing:
        url = f"{GOOGLE_UPLOAD}/{existing}"
        params = {"uploadType": "media"}
    else:
        url = GOOGLE_UPLOAD
        params = {"uploadType": "multipart"}

    if existing:
        with local.open("rb") as fh:
            r = requests.patch(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                },
                data=fh,
                timeout=600,
            )
    else:
        boundary = "archie-boundary"
        body = (
            f"--{boundary}\\r\\n"
            "Content-Type: application/json; charset=UTF-8\\r\\n\\r\\n"
            + json.dumps(metadata)
            + f"\\r\\n--{boundary}\\r\\n"
            "Content-Type: application/octet-stream\\r\\n\\r\\n"
        ).encode("utf-8") + local.read_bytes() + f"\\r\\n--{boundary}--\\r\\n".encode()

        r = requests.post(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            data=body,
            timeout=600,
        )

    r.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload")
    up.add_argument("--local", type=Path, required=True)
    up.add_argument("--name", default=None)
    up.add_argument("--dropbox", default=None)
    up.add_argument("--google-folder", default=None)

    down = sub.add_parser("download")
    down.add_argument("--output", type=Path, required=True)
    down.add_argument("--dropbox", default=None)

    args = parser.parse_args()

    if args.command == "download":
        if not dropbox_download(args.dropbox, args.output):
            print("Dropbox archive not found; starting fresh.")
            return 0
        print(f"Downloaded {args.output}")
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
