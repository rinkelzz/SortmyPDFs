import argparse
import email
import hashlib
import imaplib
import os
import re
import ssl
from datetime import datetime, timezone
from email.header import decode_header, make_header
from pathlib import Path

import requests
from dotenv import load_dotenv

# Reuse Graph auth helpers from sort_and_move.py
from sort_and_move import get_token  # noqa: E402

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

IMAP_HOST = os.getenv("IMAP_HOST")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")

ONEDRIVE_INBOX = os.getenv("ONEDRIVE_INBOX", "vomDrucker")

STATE_PATH = BASE / "state_imap.json"
SESSION = requests.Session()


def _decode_mime_words(s: str) -> str:
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _safe_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[\\/:*?\"<>|]", "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def load_state() -> dict:
    if STATE_PATH.exists():
        return json_load(STATE_PATH)
    return {"processed_uids": {}, "processed_hashes": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json_dumps(state),
        encoding="utf-8",
    )


def json_load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def json_dumps(obj: dict) -> str:
    import json

    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)


def graph_put(url: str, token: str, data: bytes, content_type: str = "application/pdf"):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }
    r = SESSION.put(url, headers=headers, data=data, timeout=180)
    return r


def upload_bytes_small(token: str, onedrive_path: str, data: bytes) -> dict:
    # PUT /content supports up to 4MB for simple upload.
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{onedrive_path}:/content"
    r = graph_put(url, token, data)
    r.raise_for_status()
    return r.json()


def upload_bytes_large(token: str, onedrive_path: str, data: bytes, chunk_size: int = 10 * 1024 * 1024) -> dict:
    # Create upload session
    create_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{onedrive_path}:/createUploadSession"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = SESSION.post(create_url, headers=headers, json={"item": {"@microsoft.graph.conflictBehavior": "rename"}}, timeout=60)
    resp.raise_for_status()
    upload_url = resp.json()["uploadUrl"]

    total = len(data)
    start = 0
    while start < total:
        end = min(start + chunk_size, total) - 1
        chunk = data[start : end + 1]
        headers = {
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{total}",
        }
        r = SESSION.put(upload_url, headers=headers, data=chunk, timeout=180)
        # 202 = accepted (more chunks), 201/200 = finished
        if r.status_code in (200, 201):
            return r.json()
        if r.status_code != 202:
            raise RuntimeError(f"Upload chunk failed: {r.status_code} {r.text[:500]}")
        start = end + 1

    raise RuntimeError("Upload session ended unexpectedly")


def upload_pdf_to_inbox(token: str, filename: str, data: bytes) -> dict:
    # Keep original filename but prefix with date to reduce collisions.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = _safe_name(filename)
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    onedrive_path = f"{ONEDRIVE_INBOX}/{today} {filename}"

    if len(data) <= 4 * 1024 * 1024:
        return upload_bytes_small(token, onedrive_path, data)
    return upload_bytes_large(token, onedrive_path, data)


def iter_pdf_attachments(msg: email.message.Message):
    for part in msg.walk():
        if part.is_multipart():
            continue
        cdisp = part.get("Content-Disposition", "") or ""
        ctype = (part.get_content_type() or "").lower()
        filename = part.get_filename()
        if filename:
            filename = _decode_mime_words(filename)

        is_attachment = "attachment" in cdisp.lower() or (filename is not None)
        is_pdf = ctype == "application/pdf" or (filename and filename.lower().endswith(".pdf"))

        if is_attachment and is_pdf:
            data = part.get_payload(decode=True) or b""
            if filename is None:
                filename = "attachment.pdf"
            yield filename, data


def require_env():
    missing = [k for k, v in {
        "IMAP_HOST": IMAP_HOST,
        "IMAP_USER": IMAP_USER,
        "IMAP_PASSWORD": IMAP_PASSWORD,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")


def main():
    parser = argparse.ArgumentParser(description="Ingest PDF attachments from IMAP and upload to OneDrive inbox (vomDrucker).")
    parser.add_argument("--delete", action="store_true", help="Delete emails after successful ingestion.")
    parser.add_argument("--all", action="store_true", help="Process all messages (default: only unseen).")
    args = parser.parse_args()

    require_env()

    state = load_state()
    processed_uids = state.setdefault("processed_uids", {})
    processed_hashes = state.setdefault("processed_hashes", {})

    token = get_token()

    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    try:
        imap.login(IMAP_USER, IMAP_PASSWORD)
        typ, _ = imap.select(IMAP_FOLDER)
        if typ != "OK":
            raise SystemExit(f"Cannot select folder: {IMAP_FOLDER}")

        criteria = "ALL" if args.all else "UNSEEN"
        typ, data = imap.uid("search", None, criteria)
        if typ != "OK":
            raise SystemExit(f"IMAP search failed: {typ} {data}")

        uids = [u for u in data[0].split() if u]
        if not uids:
            print("No messages to process.")
            return

        any_changes = False
        deleted = 0
        for uid in uids:
            uid_s = uid.decode("ascii", errors="ignore")
            if uid_s in processed_uids:
                continue

            typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                print(f"WARN: Failed to fetch UID {uid_s}")
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            pdfs = list(iter_pdf_attachments(msg))
            if not pdfs:
                # Still mark processed to avoid re-scanning forever.
                processed_uids[uid_s] = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "note": "no_pdf",
                }
                any_changes = True
                continue

            success = True
            uploaded = 0
            for filename, blob in pdfs:
                h = hashlib.sha256(blob).hexdigest()
                if h in processed_hashes:
                    continue
                try:
                    upload_pdf_to_inbox(token, filename, blob)
                    processed_hashes[h] = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "filename": filename,
                    }
                    uploaded += 1
                    any_changes = True
                except Exception as e:
                    success = False
                    print(f"ERROR: Upload failed for UID {uid_s} attachment {filename}: {e}")

            processed_uids[uid_s] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "uploaded": uploaded,
                "success": success,
            }
            any_changes = True

            if args.delete and success:
                # imaplib expects the IMAP system flag with a single backslash: \Deleted
                imap.uid("store", uid, "+FLAGS", "(\\Deleted)")
                deleted += 1

        if args.delete and deleted:
            imap.expunge()

        if any_changes:
            save_state(state)

        print(f"Done. processed={len(processed_uids)} deleted={deleted}")

    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
